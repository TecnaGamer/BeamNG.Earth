//! A bridge slab's reach past its road's edge along each deck part's whole line (server/roadmesh.py deck(): to a
//! median's middle beside another deck, narrowing to a point at a gore nose, a median barrier between opposite traffic,
//! over an exit's union), worked out at fixed stations of arc length along the part (every `step` m from its start, and
//! its end) from the whole parts alone, never from a tile's chunks: the row on a tile's cut sees both sides of it, and
//! the two tiles either side of a seam agree on it (seamprobe: per chunk, the median gaps' runs, their noses and the
//! smoothing saw only their own tile's side, steps of 2 mm to 1.1 m at the cut).
//!
//! Tile-independent within a window: only stations whose point lies in `win` (the tile box widened by the caller,
//! roads.REACH_WINDOW_M, so that the parts coming within MEDIAN_JOIN_M of them are in its list) are worked out, each
//! run of them apart; a station's result depends on the stations within about 54 m of arc length (the gaps' fill,
//! fill_pts; a nose's walk, capped at walk_pts, and its look; the smoothing), less than the window's margin, so a
//! tile's rows read the same values in every tile. A station is worked out only when both its edges lie in the window,
//! so every deck within MEDIAN_JOIN_M of a worked-out edge point is in the list, whatever the deck's width. What a deck
//! wider than 2 x (window margin - radius), about 7 m, can still get wrong at a seam: its stations whose edges pass the
//! window's edge (57.5 m out) though their centre is within the radius of the tile are left out, so a run of median
//! gaps, a gore nose or a reach fill running continuously from the seam out to them is cut there instead of at its real
//! end; the cut acts no farther back than the radius (a fill 16 m, a nose's walk 20 m and its look, the smoothing 12 m),
//! so only a median or gore running unbroken from within 54 m of the seam to 57.5 m - half the width out can differ.
//! Deterministic: no FMA, the libm crate's hypot (dnp.hypot), ordered maps only, parts in the caller's (canonical) order.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::BTreeMap;

struct Part {
    xy: Vec<[f64; 2]>,
    z: Vec<f64>,
    s: Vec<f64>,
    l: Vec<[f64; 2]>,
    r: Vec<[f64; 2]>,
    tv: Vec<[f64; 2]>,
    owner: i64,
}

fn unit(x: f64, y: f64) -> [f64; 2] {
    let h = x.dhypot(y).max(1e-9);
    [x / h, y / h]
}

/// The part's stations: (s, xy, z, half width) every `step` m of arc length from its start, and its end.
fn stations(p: &[[f64; 3]], hw: &[f64], step: f64) -> (Vec<f64>, Vec<[f64; 2]>, Vec<f64>, Vec<f64>) {
    let k = p.len();
    let mut a = vec![0.0; k];
    for i in 1..k {
        a[i] = a[i - 1] + (p[i][0] - p[i - 1][0]).dhypot(p[i][1] - p[i - 1][1]);
    }
    let tot = a[k - 1];
    if k < 2 || tot <= 1e-6 {
        return (vec![], vec![], vec![], vec![]);
    }
    let n = ((tot / step).ceil() as usize).max(1);
    let mut ts: Vec<f64> = (0..n).map(|j| j as f64 * step).collect();
    ts.push(tot);
    let (mut s, mut xy, mut z, mut h) = (vec![], vec![], vec![], vec![]);
    let mut i = 0usize;
    for &t in &ts {
        while i + 2 < k && a[i + 1] < t {
            i += 1;
        }
        let den = a[i + 1] - a[i];
        let f = if den > 0.0 { ((t - a[i]) / den).clamp(0.0, 1.0) } else { 0.0 };
        s.push(t);
        xy.push([p[i][0] + (p[i + 1][0] - p[i][0]) * f, p[i][1] + (p[i + 1][1] - p[i][1]) * f]);
        z.push(p[i][2] + (p[i + 1][2] - p[i][2]) * f);
        h.push(hw[i] + (hw[i + 1] - hw[i]) * f);
    }
    (s, xy, z, h)
}

/// roadmesh._normals: per point the miter normal (unit on a straight, longer on a bend, at most 2x).
fn normals(xy: &[[f64; 2]]) -> Vec<[f64; 2]> {
    let k = xy.len();
    let ns: Vec<[f64; 2]> = (0..k - 1).map(|i| {
        let (dx, dy) = (xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]);
        let l = dx.dhypot(dy).max(1e-9);
        [-(dy / l), dx / l]
    }).collect();
    let mut n = vec![[0.0; 2]; k];
    n[0] = ns[0];
    n[k - 1] = ns[k - 2];
    for i in 1..k - 1 {
        let m = [ns[i - 1][0] + ns[i][0], ns[i - 1][1] + ns[i][1]];
        let mut ml = m[0].dhypot(m[1]);
        if ml < 1e-9 {
            ml = 1.0;
        }
        let m = [m[0] / ml, m[1] / ml];
        let c = (m[0] * ns[i][0] + m[1] * ns[i][1]).clamp(0.5, 1.0);
        n[i] = [m[0] / c, m[1] / c];
    }
    n
}

struct Prm {
    wide: f64,
    join: f64,
    along: f64,
    along_m: f64,
    inside_m: f64,
    dz: f64,
    barrier: f64,
    nose_m: f64,
    look: usize,
    widen: f64,
    fill: usize,
    slope: f64,
    walk: usize,
    sheet_max: f64,
    sheet_tol: f64,
}

/// roadmesh._fill_short: runs of k zeros or fewer between two gaps filled by their ends' interpolation.
fn fill_short(g: &mut [f64], k: usize) {
    let nz: Vec<usize> = (0..g.len()).filter(|&i| g[i] > 0.0).collect();
    for w in nz.windows(2) {
        let (a, b) = (w[0], w[1]);
        if b - a > 1 && b - a <= k + 1 {
            let (ga, gb) = (g[a], g[b]);
            let slope = (gb - ga) / (b - a) as f64;
            for x in a + 1..b {
                g[x] = slope * (x - a) as f64 + ga;
            }
        }
    }
}

/// roadmesh._noses, a nose's walk in from its run's end capped at p.walk stations: (ext, covered).
fn noses(g: &[f64], p: &Prm) -> (Vec<f64>, Vec<bool>) {
    let k = g.len();
    let mut ext: Vec<f64> = g.iter().map(|&v| if v > 0.0 { v / 2.0 + 0.05 } else { 0.0 }).collect();
    let mut cov: Vec<bool> = g.iter().map(|&v| v > 0.0).collect();
    let lim = p.join - p.nose_m;
    let mut i = 0usize;
    while i < k {
        if g[i] <= 0.0 {
            i += 1;
            continue;
        }
        let mut j = i;
        while j + 1 < k && g[j + 1] > 0.0 {
            j += 1;
        }
        for (end, inward) in [(j as i64, -1i64), (i as i64, 1i64)] {
            let bi = (end + inward * p.look as i64).clamp(i as i64, j as i64) as usize;
            let back = g[bi];
            let ge = g[end as usize];
            if ge <= lim || ge - back < p.widen {
                continue;
            }
            let mut q = end;
            let mut steps = 0usize;
            while q >= i as i64 && q <= j as i64 && g[q as usize] > lim && steps < p.walk {
                let f = ((p.join - g[q as usize]) / p.nose_m).clamp(0.0, 1.0);
                ext[q as usize] *= f;
                cov[q as usize] = false;
                q += inward;
                steps += 1;
            }
        }
        i = j + 1;
    }
    (ext, cov)
}

/// One side's (reach past the road's edge, covered, nose) along a run of stations: _noses, _opposite, _reach and
/// _smooth_reach of roadmesh.deck / parapet_runs.
fn side_reach(g: &[f64], opp: &[bool], over: &[bool], edge: &[[f64; 2]], p: &Prm) -> (Vec<f64>, Vec<bool>, Vec<bool>) {
    let k = g.len();
    let (ext, cov) = noses(g, p);
    let mut x = vec![0.0; k];
    let mut c = vec![false; k];
    let mut nose = vec![false; k];
    for i in 0..k {
        let near = opp[i] && g[i] > 0.0 && g[i] <= p.barrier;
        let far = opp[i] && g[i] > p.barrier;
        let inside = near && over[i];
        x[i] = if inside { -(g[i] / 2.0) } else if near { g[i] / 2.0 + 0.05 } else if far { 0.0 } else { ext[i] };
        c[i] = if opp[i] { false } else { cov[i] };
        nose[i] = (g[i] > 0.0 && !c[i] && !opp[i]) || (opp[i] && g[i] <= p.barrier);
        x[i] = if x[i] < 0.0 { x[i] } else { x[i].max(p.wide) };
    }
    for i in 1..k {
        let ds = (edge[i][0] - edge[i - 1][0]).dhypot(edge[i][1] - edge[i - 1][1]) * p.slope;
        x[i] = x[i].max(x[i - 1] - ds);
    }
    for i in (0..k.saturating_sub(1)).rev() {
        let ds = (edge[i + 1][0] - edge[i][0]).dhypot(edge[i + 1][1] - edge[i][1]) * p.slope;
        x[i] = x[i].max(x[i + 1] - ds);
    }
    (x, c, nose)
}

/// Each deck part's stations (module docs): pts (N, 3) the parts' drawn lines (concatenated, off (P+1)), hw (N) the
/// deck's half width at each point. (stations (S, 8): s, x, y, z, left edge x, y, right edge x, y; soff (P+1); a part
/// under 1 um long none).
#[pyfunction]
pub fn deck_stations<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                          hw: PyReadonlyArray1<'py, f64>, step: f64)
                          -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>) {
    let (pv, ov, hv) = (pts.as_array(), off.as_array(), hw.as_array());
    let np_ = ov.len().saturating_sub(1);
    let mut soff = vec![0i64; np_ + 1];
    let mut flat: Vec<f64> = Vec::new();
    for q in 0..np_ {
        let (a, b) = (ov[q] as usize, ov[q + 1] as usize);
        let line: Vec<[f64; 3]> = (a..b).map(|i| [pv[[i, 0]], pv[[i, 1]], pv[[i, 2]]]).collect();
        let h: Vec<f64> = (a..b).map(|i| hv[i]).collect();
        let mut n = 0;
        if line.len() >= 2 {
            let (s, xy, z, hh) = stations(&line, &h, step);
            if xy.len() >= 2 {
                let nr = normals(&xy);
                for i in 0..xy.len() {
                    flat.extend_from_slice(&[s[i], xy[i][0], xy[i][1], z[i], xy[i][0] + nr[i][0] * hh[i],
                                             xy[i][1] + nr[i][1] * hh[i], xy[i][0] - nr[i][0] * hh[i],
                                             xy[i][1] - nr[i][1] * hh[i]]);
                }
                n = xy.len();
            }
        }
        soff[q + 1] = soff[q] + n as i64;
    }
    let k = flat.len() / 8;
    (numpy::ndarray::Array2::from_shape_vec((k, 8), flat).expect("stations").into_pyarray(py), soff.into_pyarray(py))
}

/// The slab's reach along each deck part's line at its stations (module docs).
/// st (S, 8), soff (P+1): deck_stations', the edges as drawn (roadmesh.trim_overlaps' cuts applied); hide (S): stations
/// no other deck's median meets (a branch's interval inside an exit's union: no slab of its own); owner (P) its id
/// (parts of one id are one deck), tdir (P) its traffic (+1 / -1 along / against the line, 0 two-way); win (4) the
/// window box; far (M, 2) the exit unions' outer edges (roadmesh._sheet_reach; far_off (F+1), far_part (F) the part's
/// index, far_side (F) +1 its left); prm: step (unused here), wide, join, along, along_m, inside_m, dz, barrier, nose_m,
/// look_pts, widen, fill_pts, slope, walk_pts, sheet_max, sheet_tol (roadmesh's constants).
/// (stations (S, 4): s, reach left, reach right (NaN outside the window), flags: 1 covered left, 2 right, 4 a nose's
/// or median barrier's point left, 8 right, 16 in the window; soff (P+1)).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_reach<'py>(py: Python<'py>, st: PyReadonlyArray2<'py, f64>, soff: PyReadonlyArray1<'py, i64>,
                       hide: PyReadonlyArray1<'py, bool>, owner: PyReadonlyArray1<'py, i64>,
                       tdir: PyReadonlyArray1<'py, f64>, win: PyReadonlyArray1<'py, f64>,
                       far: PyReadonlyArray2<'py, f64>, far_off: PyReadonlyArray1<'py, i64>,
                       far_part: PyReadonlyArray1<'py, i64>, far_side: PyReadonlyArray1<'py, f64>,
                       prm: PyReadonlyArray1<'py, f64>)
                       -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>) {
    let (sv, so, hd, own, tdv, wv) = (st.as_array(), soff.as_array(), hide.as_array(), owner.as_array(),
                                      tdir.as_array(), win.as_array());
    let (fv, fo, fp, fs, pr) = (far.as_array(), far_off.as_array(), far_part.as_array(), far_side.as_array(),
                                prm.as_array());
    let np_ = own.len();
    let p = Prm { wide: pr[1], join: pr[2], along: pr[3], along_m: pr[4], inside_m: pr[5], dz: pr[6], barrier: pr[7],
                  nose_m: pr[8], look: pr[9] as usize, widen: pr[10], fill: pr[11] as usize, slope: pr[12],
                  walk: pr[13] as usize, sheet_max: pr[14], sheet_tol: pr[15] };
    let (x0, y0, x1, y1) = (wv[0], wv[1], wv[2], wv[3]);
    let fars: Vec<(usize, f64, Vec<[f64; 2]>)> = (0..fp.len()).map(|f| {
        let (a, b) = (fo[f] as usize, fo[f + 1] as usize);
        (fp[f] as usize, fs[f], (a..b).map(|i| [fv[[i, 0]], fv[[i, 1]]]).collect())
    }).collect();
    let mut hidden: Vec<Vec<bool>> = Vec::with_capacity(np_);
    let parts: Vec<Part> = (0..np_).map(|q| {
        let (a, b) = (so[q] as usize, so[q + 1] as usize);
        hidden.push((a..b).map(|i| hd[i]).collect());
        let l: Vec<[f64; 2]> = (a..b).map(|i| [sv[[i, 4]], sv[[i, 5]]]).collect();
        let r: Vec<[f64; 2]> = (a..b).map(|i| [sv[[i, 6]], sv[[i, 7]]]).collect();
        let tv = (0..l.len()).map(|i| {
            let u = unit(l[i][0] - r[i][0], l[i][1] - r[i][1]);
            [u[1] * tdv[q], -u[0] * tdv[q]]
        }).collect();
        Part { xy: (a..b).map(|i| [sv[[i, 1]], sv[[i, 2]]]).collect(), z: (a..b).map(|i| sv[[i, 3]]).collect(),
               s: (a..b).map(|i| sv[[i, 0]]).collect(), l, r, tv, owner: own[q] }
    }).collect();
    let work = sv.shape()[0];
    let out: Vec<Vec<[f64; 4]>> = release(py, work, || {
        let inwin = |pt: &[f64; 2]| pt[0] >= x0 && pt[0] <= x1 && pt[1] >= y0 && pt[1] <= y1;
        // every edge point near the window in a grid of join-sized cells: (edge 2 x part + side, index), in order (a
        // window station's edge lies up to its half width out: the widest deck's half width more)
        let cell = p.join;
        let mut grid: BTreeMap<(i64, i64), Vec<(usize, usize)>> = BTreeMap::new();
        let mut hwmax = 0.0f64;
        for pt in &parts {
            for i in 0..pt.xy.len() {
                hwmax = hwmax.max(0.5 * (pt.l[i][0] - pt.r[i][0]).dhypot(pt.l[i][1] - pt.r[i][1]));
            }
        }
        let m = p.join + 1.0 + hwmax;
        for (q, pt) in parts.iter().enumerate() {
            for (sd, e) in [(0usize, &pt.l), (1usize, &pt.r)] {
                for (j, o) in e.iter().enumerate() {
                    if hidden[q][j] || o[0] < x0 - m || o[0] > x1 + m || o[1] < y0 - m || o[1] > y1 + m {
                        continue;
                    }
                    let key = ((o[0] / cell).floor() as i64, (o[1] / cell).floor() as i64);
                    grid.entry(key).or_default().push((2 * q + sd, j));
                }
            }
        }
        let mut out: Vec<Vec<[f64; 4]>> = Vec::with_capacity(np_);
        for (q, pt) in parts.iter().enumerate() {
            let k = pt.xy.len();
            let mut res = vec![[0.0, f64::NAN, f64::NAN, 0.0]; k];
            for i in 0..k {
                res[i][0] = pt.s[i];
            }
            // (both edges in the window: every deck within MEDIAN_JOIN_M of a worked-out edge point is in the list)
            let w: Vec<bool> = (0..pt.xy.len()).map(|i| inwin(&pt.l[i]) && inwin(&pt.r[i])).collect();
            if !w.iter().any(|&b| b) {
                out.push(res);
                continue;
            }
            let mine: Vec<[f64; 2]> = pt.tv.clone();
            // per side: gap, opposite traffic, the other's edge over this slab, the deck it faces
            let mut sides: Vec<(Vec<f64>, Vec<bool>, Vec<bool>, Vec<Option<i64>>)> = Vec::new();
            for sd in 0..2 {
                let (e, o_) = if sd == 0 { (&pt.l, &pt.r) } else { (&pt.r, &pt.l) };
                let mut g = vec![0.0; k];
                let mut opp = vec![false; k];
                let mut over = vec![false; k];
                let mut hitown: Vec<Option<i64>> = vec![None; k];
                for i in 0..k {
                    if !w[i] {
                        continue;
                    }
                    let (a, b) = if i == 0 { (0, 1) } else if i == k - 1 { (k - 2, k - 1) } else { (i - 1, i + 1) };
                    let tan = unit(e[b][0] - e[a][0], e[b][1] - e[a][1]);
                    let out_ = unit(e[i][0] - o_[i][0], e[i][1] - o_[i][1]);
                    let ex = e[i];
                    let ez = pt.z[i];
                    // the nearest point of each other deck's edge within join, at about this height: (edge, d, j)
                    let mut best: Vec<(usize, f64, usize)> = Vec::new();
                    let (cx, cy) = ((ex[0] / cell).floor() as i64, (ex[1] / cell).floor() as i64);
                    for gx in cx - 1..=cx + 1 {
                        for gy in cy - 1..=cy + 1 {
                            if let Some(v) = grid.get(&(gx, gy)) {
                                for &(oe, j) in v {
                                    let op = &parts[oe / 2];
                                    if op.owner == pt.owner {
                                        continue;
                                    }
                                    if (ez - op.z[j]).abs() >= p.dz {
                                        continue;
                                    }
                                    let oo = if oe % 2 == 0 { op.l[j] } else { op.r[j] };
                                    let d = (ex[0] - oo[0]).dhypot(ex[1] - oo[1]);
                                    if d >= p.join {
                                        continue;
                                    }
                                    match best.iter_mut().find(|b| b.0 == oe) {
                                        Some(b) => {
                                            if d < b.1 || (d == b.1 && j < b.2) {
                                                b.1 = d;
                                                b.2 = j;
                                            }
                                        }
                                        None => best.push((oe, d, j)),
                                    }
                                }
                            }
                        }
                    }
                    best.sort_by_key(|b| b.0);
                    for &(oe, d, j) in &best {
                        let op = &parts[oe / 2];
                        let oo = if oe % 2 == 0 { op.l[j] } else { op.r[j] };
                        let rel = [oo[0] - ex[0], oo[1] - ex[1]];
                        let along = (rel[0] * tan[0] + rel[1] * tan[1]).abs();
                        let ends = (i == 0 || i == k - 1) && (j == 0 || j == op.xy.len() - 1);
                        let against = mine[i][0] * op.tv[j][0] + mine[i][1] * op.tv[j][1] < -0.5;
                        let ro = rel[0] * out_[0] + rel[1] * out_[1];
                        let hit = d < p.join && (g[i] == 0.0 || d < g[i]) && along <= (p.along * d).max(p.along_m)
                            && (ro > -p.inside_m || against) && !ends;
                        if hit {
                            g[i] = d.max(1e-3);
                            opp[i] = against;
                            over[i] = ro < 0.0;
                            hitown[i] = Some(op.owner);
                        }
                    }
                }
                sides.push((g, opp, over, hitown));
            }
            let (gl0, gr0) = (sides[0].0.clone(), sides[1].0.clone());
            let mut gl = gl0.clone();
            let mut gr = gr0.clone();
            for i in 0..k {
                let same = sides[0].3[i].is_some() && sides[0].3[i] == sides[1].3[i];
                let both = gl0[i] > 0.0 && gr0[i] > 0.0 && same;
                if both && gl0[i] > gr0[i] {
                    gl[i] = 0.0;
                }
                if both && gr0[i] >= gl0[i] {
                    gr[i] = 0.0;
                }
            }
            // the exits' unions on this part: no median where the slab reaches over one (roadmesh._sheet_medians)
            let mut sl = vec![0.0f64; k];
            let mut sr = vec![0.0f64; k];
            for (fq, side, fl) in &fars {
                if *fq != q || fl.len() < 2 {
                    continue;
                }
                for i in 0..k {
                    if !w[i] {
                        continue;
                    }
                    let u = unit(pt.l[i][0] - pt.r[i][0], pt.l[i][1] - pt.r[i][1]);
                    let (e, d) = if *side > 0.0 { (pt.l[i], u) } else { (pt.r[i], [-u[0], -u[1]]) };
                    let mut tt = f64::INFINITY;
                    for sgm in fl.windows(2) {
                        let dd = [sgm[1][0] - sgm[0][0], sgm[1][1] - sgm[0][1]];
                        let den = d[0] * dd[1] - d[1] * dd[0];
                        if den.abs() <= 1e-12 {
                            continue;
                        }
                        let (wx, wy) = (sgm[0][0] - e[0], sgm[0][1] - e[1]);
                        let t = (wx * dd[1] - wy * dd[0]) / den;
                        let s = (wx * d[1] - wy * d[0]) / den;
                        let tol = p.sheet_tol / dd[0].dhypot(dd[1]).max(1e-9);
                        if s >= -tol && s <= 1.0 + tol && t >= 0.0 && t <= p.sheet_max && t < tt {
                            tt = t;
                        }
                    }
                    let tt = if tt.is_finite() { tt } else { 0.0 };
                    if *side > 0.0 {
                        sl[i] = sl[i].max(tt);
                    } else {
                        sr[i] = sr[i].max(tt);
                    }
                }
            }
            // each run of window stations apart
            let mut a = 0usize;
            while a < k {
                if !w[a] {
                    a += 1;
                    continue;
                }
                let mut b = a;
                while b < k && w[b] {
                    b += 1;
                }
                for sd in 0..2 {
                    let (gs, ss, edge) = if sd == 0 { (&gl, &sl, &pt.l) } else { (&gr, &sr, &pt.r) };
                    let mut g: Vec<f64> = gs[a..b].to_vec();
                    fill_short(&mut g, p.fill);
                    let mut opp: Vec<bool> = (a..b).map(|i| sides[sd].1[i] && g[i - a] > 0.0).collect();
                    let mut over: Vec<bool> = (a..b).map(|i| sides[sd].2[i] && g[i - a] > 0.0).collect();
                    for i in a..b {
                        if ss[i] > 0.0 {
                            g[i - a] = 0.0;
                            opp[i - a] = false;
                            over[i - a] = false;
                        }
                    }
                    let (x, c, nose) = side_reach(&g, &opp, &over, &edge[a..b], &p);
                    for i in a..b {
                        res[i][1 + sd] = x[i - a];
                        let mut f = res[i][3] as i64 | 16;
                        if c[i - a] {
                            f |= 1 << sd;
                        }
                        if nose[i - a] {
                            f |= 4 << sd;
                        }
                        res[i][3] = f as f64;
                    }
                }
                a = b;
            }
            out.push(res);
        }
        out
    });
    let mut soff2 = vec![0i64; np_ + 1];
    let mut flat: Vec<f64> = Vec::new();
    for (q, r) in out.iter().enumerate() {
        soff2[q + 1] = soff2[q] + r.len() as i64;
        for v in r {
            flat.extend_from_slice(v);
        }
    }
    let n = flat.len() / 4;
    (numpy::ndarray::Array2::from_shape_vec((n, 4), flat).expect("stations").into_pyarray(py), soff2.into_pyarray(py))
}

fn release<T, F>(py: Python<'_>, work: usize, f: F) -> T
where
    F: pyo3::marker::Ungil + FnOnce() -> T,
    T: pyo3::marker::Ungil,
{
    if work >= 4096 { py.detach(f) } else { f() }
}
