//! grading's plan-set merge: the plans of the cells a road tile, block or far tile asks for, joined once per set
//! (grading._Planner._plan_set): the pieces of one road welded to one height at their shared ends
//! (weld_continuations), a side road's end moved to the painted edge of the higher road it joins (trim_side_mouths),
//! and the segment arrays grade_points takes (PlanPack, and the ones near a box: PlanPack.near). In numpy these were
//! the server's biggest grading cost on a cold visit: a cold teleport to Santa Fe NM spent ~120 CPU-s of ~885 in them
//! (py-spy, 2026-11-11), every process merging each set of cells it asked for. Bit for bit the numpy version's numbers:
//! the same products, sums and comparisons in its order (elementwise), numpy's pairwise sums, np.interp's formula,
//! np.argmin's first minimum (a NaN first), the C library's hypot and pow (np.hypot, t ** 3), np.maximum's NaN, and
//! Python's round(x, 1) as the key of a point where plan ends meet.
use crate::dm::Dm;

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet};

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::gradesep::np_interp;
use crate::junctionsnap::np_sum;

/// np.maximum / np.minimum: a NaN either side wins.
#[inline]
fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}
#[inline]
fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a }
}
/// np.clip(x, lo, hi).
#[inline]
fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    nmin(nmax(x, lo), hi)
}

/// [0, cumsum(hypot(diff(xy)))]: the arc length at each vertex.
fn arc(xy: &[[f64; 2]]) -> Vec<f64> {
    let mut s = vec![0.0f64; xy.len()];
    for k in 1..xy.len() {
        s[k] = s[k - 1] + (xy[k][0] - xy[k - 1][0]).dhypot(xy[k][1] - xy[k - 1][1]);
    }
    s
}

/// grading._nearest_on's numbers: (distance, height, segment, t) of the point of polyline xy / z nearest (x, y)
/// (xy >= 2 vertices).
fn nearest(xy: &[[f64; 2]], z: &[f64], x: f64, y: f64) -> (f64, f64, usize, f64) {
    let m = xy.len() - 1;
    let (mut bi, mut bd, mut bt) = (0usize, f64::NAN, 0.0f64);
    for i in 0..m {
        let (a, b) = (xy[i], xy[i + 1]);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let ll = nmax(dx * dx + dy * dy, 1e-12);
        let t = clip(((x - a[0]) * dx + (y - a[1]) * dy) / ll, 0.0, 1.0);
        let (qx, qy) = (a[0] + dx * t, a[1] + dy * t);
        let d = (qx - x).dhypot(qy - y);
        if i == 0 || d < bd || d.is_nan() {
            bi = i;
            bd = d;
            bt = t;
            if d.is_nan() {
                break; // (np.argmin: the first NaN)
            }
        }
    }
    (bd, z[bi] + (z[bi + 1] - z[bi]) * bt, bi, bt)
}

/// grading._nearest_on: (distance, height, arc position from the nearer end) of the point of a plan's centreline xy
/// (n, 2+) / z (n,) nearest (x, y).
#[pyfunction]
pub fn plan_nearest_on(xy: PyReadonlyArray2<'_, f64>, z: PyReadonlyArray1<'_, f64>, x: f64, y: f64)
                       -> PyResult<(f64, f64, f64)> {
    let (a, zz) = (xy.as_array(), z.as_array());
    if a.nrows() < 2 || a.ncols() < 2 || zz.len() != a.nrows() {
        return Err(PyValueError::new_err("plan_nearest_on: a plan of 2+ vertices, z for each"));
    }
    let p: Vec<[f64; 2]> = (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect();
    let zv: Vec<f64> = zz.iter().copied().collect();
    let (d, zq, i, t) = nearest(&p, &zv, x, y);
    let seg: Vec<f64> = p.windows(2).map(|w| {
        let (dx, dy) = (w[1][0] - w[0][0], w[1][1] - w[0][1]);
        nmax(dx * dx + dy * dy, 1e-12).sqrt()
    }).collect();
    let s = np_sum(&seg[..i]) + seg[i] * t;
    let rest = np_sum(&seg) - s;
    Ok((d, zq, if rest < s { rest } else { s }))
}

/// The plans as the kernels take them: every plan's vertices one after another (xy (n, 2), z (n,)), plan j's from
/// off[j] to off[j + 1].
struct Plans {
    xy: Vec<[f64; 2]>,
    z: Vec<f64>,
    off: Vec<usize>,
}

impl Plans {
    fn read(xy: &PyReadonlyArray2<'_, f64>, off: &PyReadonlyArray1<'_, i64>, z: &PyReadonlyArray1<'_, f64>,
            what: &str) -> PyResult<Plans> {
        let (a, o, zz) = (xy.as_array(), off.as_array(), z.as_array());
        if a.ncols() < 2 || zz.len() != a.nrows() || o.is_empty() || o[0] != 0 || o[o.len() - 1] as usize != a.nrows()
            || (1..o.len()).any(|i| o[i] < o[i - 1]) {
            return Err(PyValueError::new_err(format!("{what}: mismatched plan arrays")));
        }
        Ok(Plans {
            xy: (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect(),
            z: zz.iter().copied().collect(),
            off: o.iter().map(|&v| v as usize).collect(),
        })
    }
    fn n(&self) -> usize {
        self.off.len() - 1
    }
    fn xy(&self, j: usize) -> &[[f64; 2]] {
        &self.xy[self.off[j]..self.off[j + 1]]
    }
    fn z(&self, j: usize) -> &[f64] {
        &self.z[self.off[j]..self.off[j + 1]]
    }
}

/// grading.weld_continuations: one road planned in pieces meets itself at the higher end's height at each shared end,
/// eased over taper m along the lower piece. Plans (xy, off, z); abut (plans, 2): an end that is a bridge abutment
/// (left alone); rank (plans,): grade_rank (below 0: none). Returns (z, changed per plan).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn plan_weld<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                      z: PyReadonlyArray1<'py, f64>, abut: PyReadonlyArray2<'py, bool>, rank: PyReadonlyArray1<'py, i64>,
                      join_m: f64, max_step: f64, taper: f64)
                      -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<bool>>)> {
    let p = Plans::read(&xy, &off, &z, "plan_weld")?;
    let n = p.n();
    let (abut, rank) = (abut.as_array(), rank.as_array());
    if abut.nrows() != n || abut.ncols() != 2 || rank.len() != n {
        return Err(PyValueError::new_err("plan_weld: mismatched plan arrays"));
    }
    let abut: Vec<[bool; 2]> = (0..n).map(|j| [abut[[j, 0]], abut[[j, 1]]]).collect();
    let rank: Vec<i64> = rank.iter().copied().collect();
    let got = py.detach(|| weld_core(&p, &abut, &rank, join_m, max_step, taper)).map_err(PyValueError::new_err)?;
    Ok((got.0.into_pyarray(py), got.1.into_pyarray(py)))
}

/// plan_weld's work: (z, changed per plan).
fn weld_core(p: &Plans, abut: &[[bool; 2]], rank: &[i64], join_m: f64, max_step: f64, taper: f64)
             -> Result<(Vec<f64>, Vec<bool>), String> {
    let n = p.n();
    {
        // (i, e, end point, end height) of every plan end that is no abutment
        let mut ends: Vec<(usize, usize, [f64; 2], f64)> = Vec::new();
        for i in 0..n {
            let (q, zz) = (p.xy(i), p.z(i));
            if q.len() < 2 {
                continue;
            }
            for e in 0..2 {
                if abut[i][e] {
                    continue;
                }
                let k = if e == 0 { 0 } else { q.len() - 1 };
                ends.push((i, e, q[k], zz[k]));
            }
        }
        let cell = |q: [f64; 2]| -> Result<(i64, i64), String> {
            let (fx, fy) = ((q[0] / join_m).floor(), (q[1] / join_m).floor());
            if !fx.is_finite() || !fy.is_finite() {
                return Err("plan_weld: a plan end off the grid (math.floor)".into());
            }
            Ok((fx as i64, fy as i64))
        };
        let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
        for (a, end) in ends.iter().enumerate() {
            grid.entry(cell(end.2)?).or_default().push(a);
        }
        let mut target: Vec<(usize, usize, f64)> = Vec::new();
        for &(i, e, q, ze) in &ends {
            if rank[i] < 0 {
                continue;
            }
            let mut hi = ze;
            let (gx, gy) = cell(q)?;
            for cx in [gx - 1, gx, gx + 1] {
                for cy in [gy - 1, gy, gy + 1] {
                    if let Some(bs) = grid.get(&(cx, cy)) {
                        for &b in bs {
                            let (j, _f, q2, z2) = ends[b];
                            if i == j || rank[j] != rank[i] {
                                continue;
                            }
                            if (q[0] - q2[0]).dhypot(q[1] - q2[1]) > join_m || (z2 - ze).abs() > max_step {
                                continue;
                            }
                            if z2 > hi {
                                hi = z2;
                            }
                        }
                    }
                }
            }
            if hi > ze + 1e-6 {
                target.push((i, e, hi));
            }
        }
        let mut zout = p.z.clone();
        let mut changed = vec![false; n];
        if target.is_empty() {
            return Ok((zout, changed));
        }
        // plan -> its vertices' added heights (summed in the targets' order) and which were touched
        let mut add: HashMap<usize, (Vec<f64>, Vec<bool>)> = HashMap::new();
        let mut arcs: HashMap<usize, Vec<f64>> = HashMap::new();
        let mut delta: Vec<f64> = Vec::new();
        for &(i, e, hi) in &target {
            let zz = p.z(i);
            let z_end = if e == 0 { zz[0] } else { zz[zz.len() - 1] };
            let s = arcs.entry(i).or_insert_with(|| arc(p.xy(i)));
            let last = s[s.len() - 1];
            delta.clear();
            let mut any = false;
            for &sk in s.iter() {
                let d = if e == 0 { sk } else { last - sk };
                let u = clip(d / taper, 0.0, 1.0);
                let w = 1.0 - u * u * (3.0 - 2.0 * u);
                let dl = (hi - z_end) * w;
                any |= dl.abs() > 1e-6;
                delta.push(dl);
            }
            if !any {
                continue;
            }
            let acc = add.entry(i).or_insert_with(|| (vec![0.0; zz.len()], vec![false; zz.len()]));
            for (k, &dl) in delta.iter().enumerate() {
                let big = dl.abs() > 1e-6;
                acc.0[k] += if big { dl } else { 0.0 };
                acc.1[k] |= big;
            }
        }
        for (i, (acc, hit)) in add {
            let o = p.off[i];
            for k in 0..acc.len() {
                if hit[k] {
                    zout[o + k] += acc[k];
                }
            }
            changed[i] = true;
        }
        Ok((zout, changed))
    }
}

/// A box grid over the plans' boxes for the point-in-box candidate queries (each box in every cell it covers, a box
/// over too many cells in a list asked always); a query's candidates come back ascending, tested against the plans'
/// current boxes by the caller.
struct BoxGrid {
    x0: f64,
    y0: f64,
    cell: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
    big: Vec<u32>,
}

impl BoxGrid {
    const MAX_CELLS_PER_BOX: usize = 4096;

    fn new(boxes: &[[f64; 4]], use_: &[bool]) -> BoxGrid {
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for (k, b) in boxes.iter().enumerate() {
            if use_[k] && b.iter().all(|v| v.is_finite()) {
                x0 = x0.min(b[0]);
                y0 = y0.min(b[1]);
                x1 = x1.max(b[2]);
                y1 = y1.max(b[3]);
            }
        }
        let mut g = BoxGrid { x0: 0.0, y0: 0.0, cell: 64.0, nx: 0, ny: 0, cells: Vec::new(), big: Vec::new() };
        if !(x1 >= x0 && y1 >= y0) {
            for (k, b) in boxes.iter().enumerate() {
                if use_[k] && !b.iter().all(|v| v.is_finite()) {
                    g.big.push(k as u32);
                }
            }
            return g;
        }
        g.cell = 64.0f64.max((x1 - x0).max(y1 - y0) / 256.0);
        g.x0 = x0;
        g.y0 = y0;
        g.nx = ((x1 - x0) / g.cell) as usize + 1;
        g.ny = ((y1 - y0) / g.cell) as usize + 1;
        g.cells = vec![Vec::new(); g.nx * g.ny];
        for (k, b) in boxes.iter().enumerate() {
            if use_[k] {
                g.add(k, b);
            }
        }
        g
    }

    fn add(&mut self, k: usize, b: &[f64; 4]) {
        if !b.iter().all(|v| v.is_finite()) || self.nx == 0 {
            if b.iter().any(|v| v.is_nan()) {
                return; // (a NaN box holds no point)
            }
            self.big.push(k as u32);
            return;
        }
        let (Some(i0), Some(i1), Some(j0), Some(j1)) = (self.ix(b[0]), self.ix(b[2]), self.iy(b[1]), self.iy(b[3]))
        else {
            self.big.push(k as u32);
            return;
        };
        if (i1 - i0 + 1) * (j1 - j0 + 1) > Self::MAX_CELLS_PER_BOX {
            self.big.push(k as u32);
            return;
        }
        for j in j0..=j1 {
            for i in i0..=i1 {
                self.cells[j * self.nx + i].push(k as u32);
            }
        }
    }

    /// The cell column of x, clamped into the grid (None: NaN).
    fn ix(&self, x: f64) -> Option<usize> {
        let v = ((x - self.x0) / self.cell).floor();
        if v.is_nan() { None } else { Some(v.clamp(0.0, (self.nx - 1) as f64) as usize) }
    }
    fn iy(&self, y: f64) -> Option<usize> {
        let v = ((y - self.y0) / self.cell).floor();
        if v.is_nan() { None } else { Some(v.clamp(0.0, (self.ny - 1) as f64) as usize) }
    }

    /// The plans whose (registered) boxes may hold q, ascending, each once.
    fn query(&self, q: [f64; 2], out: &mut Vec<u32>) {
        out.clear();
        out.extend_from_slice(&self.big);
        // (a point off the grid: its edge cell, where every box reaching past that edge is clamped in too)
        if let (true, Some(i), Some(j)) = (self.nx > 0, self.ix(q[0]), self.iy(q[1])) {
            out.extend_from_slice(&self.cells[j * self.nx + i]);
        }
        out.sort_unstable();
        out.dedup();
    }
}

/// A Plan's bbox (grading.Plan / set_widen): its vertices' extent grown by half + t_max, or by half + the most it
/// widens + t_max when it widens.
fn plan_bbox(xy: &[[f64; 2]], half: f64, wmax: Option<f64>, t_max: f64) -> [f64; 4] {
    let pad = match wmax { None => half + t_max, Some(w) => half + w + t_max };
    let (mut x0, mut y0, mut x1, mut y1) = (xy[0][0], xy[0][1], xy[0][0], xy[0][1]);
    for q in &xy[1..] {
        x0 = nmin(x0, q[0]);
        y0 = nmin(y0, q[1]);
        x1 = nmax(x1, q[0]);
        y1 = nmax(y1, q[1]);
    }
    [x0 - pad, y0 - pad, x1 + pad, y1 + pad]
}

/// Where trim_core cut a plan: its line taken in the order of the end trimmed (rev: its last end), the new first
/// vertex f of the way from vertex j - 1 to j, then vertices k0.. (of n0).
#[derive(Clone, Copy)]
struct TrimAt {
    rev: bool,
    j: usize,
    f: f64,
    k0: usize,
    n0: usize,
}

/// A per-vertex array of a plan (its bank, its widen) cut as trim_core cut its line (None: not as long as the line
/// was, left as it is).
fn trim_vals(v: &[f64], at: &TrimAt) -> Option<Vec<f64>> {
    if v.len() != at.n0 {
        return None;
    }
    let q: Vec<f64> = if at.rev { v.iter().rev().copied().collect() } else { v.to_vec() };
    let (a, b) = (q[at.j - 1], q[at.j]);
    let mut out = Vec::with_capacity(q.len() - at.k0 + 1);
    out.push(a + (b - a) * at.f);
    out.extend_from_slice(&q[at.k0..]);
    if at.rev {
        out.reverse();
    }
    Some(out)
}

/// The most a widen widens (None: no widen, an empty one).
fn widen_max(w: &Option<Vec<f64>>) -> Option<f64> {
    match w {
        Some(w) if !w.is_empty() => Some(w.iter().copied().fold(f64::NEG_INFINITY, f64::max)),
        _ => None,
    }
}

/// A plan's new line and heights and where it was cut (None: as it was).
type Over = Vec<Option<(Vec<[f64; 2]>, Vec<f64>, TrimAt)>>;

/// plan_trim_mouths' work: (trimmed plans, the end trimmed of each, every plan's new line and heights); boxes updated.
#[allow(clippy::too_many_arguments)]
fn trim_core(p: &Plans, half: &[f64], road_half: &[f64], abut: &[[bool; 2]], rank: &[i64], boxes: &mut [[f64; 4]],
             widens: &[Option<Vec<f64>>], reach: f64, ramp_m: f64, t_max: f64) -> (Vec<usize>, Vec<usize>, Over) {
    let n = p.n();
    {
        let longs: Vec<bool> = (0..n).map(|j| p.xy(j).len() >= 2).collect();
        let mut grid = BoxGrid::new(boxes, &longs);
        // (a trimmed plan's new line and heights)
        let mut over: Over = vec![None; n];
        let mut out_i = Vec::new();
        let mut out_e = Vec::new();
        let mut cand: Vec<u32> = Vec::new();
        for i in 0..n {
            let (pxy, pz) = (p.xy(i), p.z(i));
            if rank[i] < 0 || pxy.len() < 2 {
                continue;
            }
            for e in 0..2usize {
                if abut[i][e] {
                    continue;
                }
                let q = if e == 0 { pxy[0] } else { pxy[pxy.len() - 1] };
                let mut best: Option<(f64, f64, usize)> = None;
                grid.query(q, &mut cand);
                for &k in &cand {
                    let k = k as usize;
                    let b = boxes[k];
                    if k == i || !longs[k] || rank[k] <= rank[i]
                        || !(b[0] <= q[0] && q[0] <= b[2] && b[1] <= q[1] && q[1] <= b[3]) {
                        continue;
                    }
                    let (kxy, kz) = match &over[k] {
                        Some((a, b, _)) => (a.as_slice(), b.as_slice()),
                        None => (p.xy(k), p.z(k)),
                    };
                    let (d, zq, _, _) = nearest(kxy, kz, q[0], q[1]);
                    if d <= half[k] + reach && best.is_none_or(|b| d < b.0) {
                        best = Some((d, zq, k));
                    }
                }
                let Some((d0, zq, k)) = best else { continue };
                let z_e = if e == 0 { pz[0] } else { pz[pz.len() - 1] };
                if d0 > road_half[k] + 0.25 || (z_e - zq).abs() > 0.05 {
                    continue;
                }
                let (kxy, kz) = match &over[k] {
                    Some((a, b, _)) => (a.as_slice(), b.as_slice()),
                    None => (p.xy(k), p.z(k)),
                };
                let (qq, zs): (Vec<[f64; 2]>, Vec<f64>) = if e == 0 {
                    (pxy.to_vec(), pz.to_vec())
                } else {
                    (pxy.iter().rev().copied().collect(), pz.iter().rev().copied().collect())
                };
                let rh = road_half[k];
                // (up to the first vertex out of the carriageway: the rest unused)
                let mut dist = Vec::new();
                let mut j: isize = -1;
                for a in &qq {
                    let d = nearest(kxy, kz, a[0], a[1]).0;
                    dist.push(d);
                    if d >= rh {
                        j = dist.len() as isize - 1;
                        break;
                    }
                }
                if j <= 0 {
                    continue; // already outside / no crossing
                }
                let j = j as usize;
                let (a, b) = (qq[j - 1], qq[j]);
                let (da, db) = (dist[j - 1], dist[j]);
                let f = if db == da { 0.0 } else { (rh - da) / (db - da) };
                let c = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
                let z_edge = nearest(kxy, kz, c[0], c[1]).1;
                let mut k0 = j;
                if (qq[j][0] - c[0]).dhypot(qq[j][1] - c[1]) < 0.05 {
                    k0 += 1;
                }
                if k0 >= qq.len() {
                    continue;
                }
                let mut nxy = Vec::with_capacity(qq.len() - k0 + 1);
                nxy.push(c);
                nxy.extend_from_slice(&qq[k0..]);
                let mut nz = Vec::with_capacity(nxy.len());
                nz.push(z_edge);
                nz.extend_from_slice(&zs[k0..]);
                // (Hermite from the edge to its own profile over ramp_m: its own grade at the far end)
                let sr = arc(&nxy);
                let last = sr[sr.len() - 1];
                let ramp = if last < ramp_m { last } else { ramp_m };
                if ramp > 1.0 {
                    let z1 = np_interp(ramp, &sr, &nz);
                    let back = ramp - 0.5;
                    let z_a = np_interp(if back > 0.0 { back } else { 0.0 }, &sr, &nz);
                    let slope = (z1 - z_a) / 0.5;
                    for (kk, &s) in sr.iter().enumerate() {
                        if s <= ramp {
                            let t = clip(s / ramp, 0.0, 1.0);
                            let (t2, t3) = (t * t, t.dpowf(3.0));
                            let h00 = 2.0 * t3 - 3.0 * t2 + 1.0;
                            let h10 = t3 - 2.0 * t2 + t;
                            let h01 = -2.0 * t3 + 3.0 * t2;
                            let h11 = t3 - t2;
                            nz[kk] = h00 * z_edge + h10 * 0.0 * ramp + h01 * z1 + h11 * slope * ramp;
                        }
                    }
                }
                if e == 1 {
                    nxy.reverse();
                    nz.reverse();
                }
                let at = TrimAt { rev: e == 1, j, f, k0, n0: qq.len() };
                // (its box as set_widen makes it from the cut widen)
                let wm = match &widens[i] {
                    Some(w) => match trim_vals(w, &at) {
                        Some(tw) => widen_max(&Some(tw)),
                        None => widen_max(&widens[i]),
                    },
                    None => None,
                };
                let nb = plan_bbox(&nxy, half[i], wm, t_max);
                let inside = (0..4).all(|c| if c < 2 { nb[c] >= boxes[i][c] } else { nb[c] <= boxes[i][c] });
                boxes[i] = nb;
                if !inside {
                    grid.add(i, &nb);
                }
                over[i] = Some((nxy, nz, at));
                out_i.push(i);
                out_e.push(e);
                break;
            }
        }
        (out_i, out_e, over)
    }
}

/// Python's round(x, 1) as a key: the integer nearest x * 10 taken exactly (half to even), the decimal Python
/// rounds to (its correctly rounded repr); None for a NaN or infinity (a key of its own).
fn round1_key(x: f64) -> Option<i64> {
    if !x.is_finite() || x.abs() > 1e14 {
        return None;
    }
    let p = x * 10.0;
    // (the exact error of the product: Veltkamp's split of x, 10 exact in one half)
    let c = 134217729.0 * x; // 2^27 + 1
    let xh = c - (c - x);
    let xl = x - xh;
    let e = (xh * 10.0 - p) + xl * 10.0;
    let k = p.round();
    let a = p - k;
    let mut r = k;
    if a == -0.5 {
        // p = k - 0.5: the exact value k - 0.5 + e
        if e < 0.0 {
            r = k - 1.0;
        } else if e == 0.0 && (k - 1.0) % 2.0 == 0.0 {
            r = k - 1.0;
        }
    } else if a == 0.5 {
        // p = k + 0.5
        if e > 0.0 {
            r = k + 1.0;
        } else if e == 0.0 && (k + 1.0) % 2.0 == 0.0 {
            r = k + 1.0;
        }
    }
    Some(r as i64)
}

/// The groups of plan ends at one point (Python's round(x, 1), round(y, 1) of each), in the order each point was
/// first met.
fn end_groups<T>(items: Vec<([f64; 2], T)>) -> Vec<Vec<T>> {
    let mut at: HashMap<(i64, i64), usize> = HashMap::new();
    let mut groups: Vec<Vec<T>> = Vec::new();
    for (q, it) in items {
        match (round1_key(q[0]), round1_key(q[1])) {
            (Some(kx), Some(ky)) => {
                let g = *at.entry((kx, ky)).or_insert_with(|| {
                    groups.push(Vec::new());
                    groups.len() - 1
                });
                groups[g].push(it);
            }
            _ => groups.push(vec![it]),
        }
    }
    groups
}

#[derive(PartialEq)]
struct Step(f64, usize, usize);
impl Eq for Step {}
impl PartialOrd for Step {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Step {
    // (a min-heap of Python's (acc, plan, end) tuples)
    fn cmp(&self, o: &Self) -> Ordering {
        o.0.partial_cmp(&self.0).unwrap_or(Ordering::Equal).then(o.1.cmp(&self.1)).then(o.2.cmp(&self.2))
    }
}

/// grading.PlanPack: the plans' segment arrays as grade_points takes them (each plan without its zero-length segments,
/// _dedupe_plan; plans under 2 vertices left out). Plans (xy, off, z); bank, widen: the plans' arrays, plan j's
/// bank_off[j]..bank_off[j + 1] (has_bank[j] false: none, zeros), widen's the same. numpy's concatenation of them taken
/// as it is: a plan's bank as long as it is (trim_side_mouths keeps the untrimmed plan's), read at the segments' vertex
/// positions in the concatenated plans (as PlanPack's bk[ia]: one longer shifts the plans after it). half, road_half (plans,); rid (plans,): its road (-1: none); rank (plans,); abut (plans, 2); steep (plans,);
/// bbox (plans, 4); cut (c, 6) the plans' cut rows, plan j's cut_off[j]..cut_off[j + 1] (has_cut[j] false: it has
/// none); then grading's constants. Returns a dict of PlanPack's attributes.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn plan_pack<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                      z: PyReadonlyArray1<'py, f64>, bank: PyReadonlyArray1<'py, f64>,
                      bank_off: PyReadonlyArray1<'py, i64>, has_bank: PyReadonlyArray1<'py, bool>,
                      widen: PyReadonlyArray1<'py, f64>, widen_off: PyReadonlyArray1<'py, i64>,
                      has_widen: PyReadonlyArray1<'py, bool>, half: PyReadonlyArray1<'py, f64>,
                      road_half: PyReadonlyArray1<'py, f64>, rid: PyReadonlyArray1<'py, i64>,
                      rank: PyReadonlyArray1<'py, i64>, abut: PyReadonlyArray2<'py, bool>,
                      steep: PyReadonlyArray1<'py, bool>, bbox: PyReadonlyArray2<'py, f64>,
                      cut: PyReadonlyArray2<'py, f64>, cut_off: PyReadonlyArray1<'py, i64>,
                      has_cut: PyReadonlyArray1<'py, bool>, dupe_m: f64,
                      cont_cos_min: f64, cont_join: f64, link_cos_min: f64, abut_near: f64, t_max: f64,
                      leg_shift: u32, span_m: f64, turn_deg: f64) -> PyResult<Bound<'py, PyDict>> {
    let p = Plans::read(&xy, &off, &z, "plan_pack")?;
    let inp = read_pack_in(p.n(), &bank, &bank_off, &has_bank, &widen, &widen_off, &has_widen, &half, &road_half, &rid,
                           &rank, &abut, &steep, &bbox, &cut, &cut_off, &has_cut)?;
    let k = PackConsts { dupe_m, cont_cos_min, cont_join, link_cos_min, abut_near, t_max, leg_shift, span_m, turn_deg };
    let res = py.detach(|| pack_core(&p, &inp, &k)).map_err(PyValueError::new_err)?;
    pack_dict(py, res)
}

/// plan_pack's per-plan inputs read: the plans' banks (bank_off: plan j's bank_off[j]..bank_off[j + 1], has_bank[j]
/// false: none), widens the same, cut rows the same, and their other arrays.
#[allow(clippy::too_many_arguments)]
fn read_pack_in<'py>(np_: usize, bank: &PyReadonlyArray1<'py, f64>, bank_off: &PyReadonlyArray1<'py, i64>,
                     has_bank: &PyReadonlyArray1<'py, bool>, widen: &PyReadonlyArray1<'py, f64>,
                     widen_off: &PyReadonlyArray1<'py, i64>, has_widen: &PyReadonlyArray1<'py, bool>,
                     half: &PyReadonlyArray1<'py, f64>, road_half: &PyReadonlyArray1<'py, f64>,
                     rid: &PyReadonlyArray1<'py, i64>, rank: &PyReadonlyArray1<'py, i64>,
                     abut: &PyReadonlyArray2<'py, bool>, steep: &PyReadonlyArray1<'py, bool>,
                     bbox: &PyReadonlyArray2<'py, f64>, cut: &PyReadonlyArray2<'py, f64>,
                     cut_off: &PyReadonlyArray1<'py, i64>, has_cut: &PyReadonlyArray1<'py, bool>)
                     -> PyResult<PackIn> {
    let (bank, widen, half, road_half, rid, rank, abut, steep, bbox, cut, cut_off) =
        (bank.as_array(), widen.as_array(), half.as_array(), road_half.as_array(), rid.as_array(), rank.as_array(),
         abut.as_array(), steep.as_array(), bbox.as_array(), cut.as_array(), cut_off.as_array());
    let offs_ok = |o: &PyReadonlyArray1<'py, i64>, len: usize, flags: &PyReadonlyArray1<'py, bool>| {
        let (o, f) = (o.as_array(), flags.as_array());
        o.len() == np_ + 1 && f.len() == np_ && o[0] == 0 && o[np_] as usize == len
            && (1..o.len()).all(|i| o[i] >= o[i - 1])
    };
    if !offs_ok(bank_off, bank.len(), has_bank) || !offs_ok(widen_off, widen.len(), has_widen)
        || half.len() != np_ || road_half.len() != np_
        || rid.len() != np_ || rank.len() != np_ || abut.nrows() != np_ || abut.ncols() != 2 || steep.len() != np_
        || bbox.nrows() != np_ || bbox.ncols() != 4 || cut.ncols() != 6 || cut_off.len() != np_ + 1
        || has_cut.as_array().len() != np_ || cut_off.iter().any(|&o| o < 0 || o as usize > cut.nrows())
        || (1..cut_off.len()).any(|i| cut_off[i] < cut_off[i - 1]) {
        return Err(PyValueError::new_err("plan_pack: mismatched plan arrays"));
    }
    // (each plan's own bank and widen, None: none)
    let per = |v: numpy::ndarray::ArrayView1<f64>, o: &PyReadonlyArray1<'py, i64>, f: &PyReadonlyArray1<'py, bool>| {
        let (o, f) = (o.as_array(), f.as_array());
        (0..np_).map(|j| if f[j] {
            Some(v.slice(numpy::ndarray::s![o[j] as usize..o[j + 1] as usize]).to_vec())
        } else { None }).collect::<Vec<Option<Vec<f64>>>>()
    };
    let banks = per(bank, bank_off, has_bank);
    let widens = per(widen, widen_off, has_widen);
    let cutv: Vec<[f64; 6]> = (0..cut.nrows()).map(|r| std::array::from_fn(|c| cut[[r, c]])).collect();
    // (each plan's cut rows: its segments')
    let has_cut = has_cut.as_array();
    let cuts: Vec<Option<Vec<[f64; 6]>>> = (0..np_).map(|j| if has_cut[j] {
        Some(cutv[cut_off[j] as usize..cut_off[j + 1] as usize].to_vec())
    } else { None }).collect();
    Ok(PackIn {
        banks, widens, cuts,
        half: half.iter().copied().collect(),
        road_half: road_half.iter().copied().collect(),
        rid: rid.iter().copied().collect(),
        rank: rank.iter().copied().collect(),
        abut: (0..np_).map(|j| [abut[[j, 0]], abut[[j, 1]]]).collect(),
        steep: steep.iter().copied().collect(),
        bbox: (0..np_).map(|j| [bbox[[j, 0]], bbox[[j, 1]], bbox[[j, 2]], bbox[[j, 3]]]).collect(),
    })
}

/// plan_pack's per-plan inputs.
struct PackIn {
    banks: Vec<Option<Vec<f64>>>,
    widens: Vec<Option<Vec<f64>>>,
    cuts: Vec<Option<Vec<[f64; 6]>>>,
    half: Vec<f64>,
    road_half: Vec<f64>,
    rid: Vec<i64>,
    rank: Vec<i64>,
    abut: Vec<[bool; 2]>,
    steep: Vec<bool>,
    bbox: Vec<[f64; 4]>,
}

/// grading's constants for plan_pack.
struct PackConsts {
    dupe_m: f64,
    cont_cos_min: f64,
    cont_join: f64,
    link_cos_min: f64,
    abut_near: f64,
    t_max: f64,
    leg_shift: u32,
    span_m: f64,
    turn_deg: f64,
}

/// PlanPack's arrays (None: no plan of 2+ vertices).
type PackOut = Option<(usize, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<i64>,
                       Vec<i64>, Vec<u8>, Vec<f64>, Vec<f64>, Vec<u8>, Vec<u8>, Vec<f64>, Vec<i64>, Vec<i64>)>;

/// A plan as PlanPack packs it (_dedupe_plan's): plan k's arrays.
struct Row {
    k: usize,
    xy: Vec<[f64; 2]>,
    z: Vec<f64>,
    bank: Vec<f64>,
    widen: Vec<f64>,
    cut: Option<Vec<[f64; 6]>>,
    bbox: [f64; 4],
}

/// plan_pack's work.
fn pack_core(p: &Plans, inp: &PackIn, k: &PackConsts) -> Result<PackOut, String> {
    let np_ = p.n();
    let PackIn { banks, widens, cuts, half, road_half, rid, rank, abut, steep, bbox } = inp;
    let PackConsts { dupe_m, cont_cos_min, cont_join, link_cos_min, abut_near, t_max, leg_shift, span_m, turn_deg } = *k;
    {
        // _dedupe_plan
        let mut rows: Vec<Row> = Vec::new();
        for j in 0..np_ {
            let (q, zz) = (p.xy(j), p.z(j));
            let n = q.len();
            if n < 2 {
                continue;
            }
            let seg_ok: Vec<bool> = q.windows(2).map(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1]) > dupe_m)
                .collect();
            if seg_ok.iter().all(|&v| v) {
                rows.push(Row { k: j, xy: q.to_vec(), z: zz.to_vec(),
                                bank: banks[j].clone().unwrap_or_else(|| vec![0.0; n]),
                                widen: widens[j].clone().unwrap_or_else(|| vec![0.0; n]), cut: cuts[j].clone(),
                                bbox: bbox[j] });
                continue;
            }
            let mut keep = vec![0usize];
            for i in 1..n {
                let l = *keep.last().unwrap();
                if (q[i][0] - q[l][0]).dhypot(q[i][1] - q[l][1]) > dupe_m {
                    keep.push(i);
                } else {
                    *keep.last_mut().unwrap() = i;
                }
            }
            if keep.len() < 2 {
                continue;
            }
            let nxy: Vec<[f64; 2]> = keep.iter().map(|&i| q[i]).collect();
            // (np.asarray(cut)[seg_ok]: a row per segment)
            let c = match &cuts[j] {
                Some(c) if c.len() != seg_ok.len() => {
                    return Err(format!("plan_pack: {} cut rows for {} segments", c.len(), seg_ok.len()));
                }
                Some(c) => Some(c.iter().zip(&seg_ok).filter(|(_, &ok)| ok).map(|(r, _)| *r).collect::<Vec<_>>()),
                None => None,
            };
            let bb = plan_bbox(&nxy, half[j], None, t_max);
            // (bk[keep] of its own bank: an IndexError past its end)
            let pick = |v: &Option<Vec<f64>>| -> Result<Vec<f64>, String> {
                match v {
                    None => Ok(vec![0.0; keep.len()]),
                    Some(v) => keep.iter().map(|&i| v.get(i).copied()
                        .ok_or_else(|| "plan_pack: a bank / widen shorter than its plan".to_string())).collect(),
                }
            };
            rows.push(Row { k: j, z: keep.iter().map(|&i| zz[i]).collect(), bank: pick(&banks[j])?,
                            widen: pick(&widens[j])?, cut: c, bbox: bb, xy: nxy });
        }
        let n = rows.len();
        if n == 0 {
            return Ok(None);
        }
        let nseg: Vec<usize> = rows.iter().map(|r| r.xy.len() - 1).collect();
        let mut s0 = vec![0usize; n + 1];
        for r in 0..n {
            s0[r + 1] = s0[r] + nseg[r];
        }
        let ns = s0[n];
        let mut a = Vec::with_capacity(ns * 2);
        let mut b = Vec::with_capacity(ns * 2);
        let (mut za, mut zb, mut ba, mut bb) = (Vec::with_capacity(ns), Vec::with_capacity(ns), Vec::with_capacity(ns),
                                                Vec::with_capacity(ns));
        let (mut h, mut rh, mut ws) = (Vec::with_capacity(ns), Vec::with_capacity(ns), Vec::with_capacity(ns));
        // (np.concatenate of the rows' banks / widens, read at the vertices' positions in the concatenated rows)
        let bk: Vec<f64> = rows.iter().flat_map(|r| r.bank.iter().copied()).collect();
        let wd: Vec<f64> = rows.iter().flat_map(|r| r.widen.iter().copied()).collect();
        let any_w = wd.iter().any(|&w| w != 0.0);
        let mut g = 0usize;
        for r in &rows {
            let j = r.k;
            for s in 0..r.xy.len() - 1 {
                a.push(r.xy[s][0]);
                a.push(r.xy[s][1]);
                b.push(r.xy[s + 1][0]);
                b.push(r.xy[s + 1][1]);
                za.push(r.z[s]);
                zb.push(r.z[s + 1]);
                let (Some(&b0), Some(&b1), Some(&w0), Some(&w1)) = (bk.get(g + s), bk.get(g + s + 1), wd.get(g + s),
                                                                     wd.get(g + s + 1))
                else {
                    return Err("plan_pack: the banks / widens shorter than the plans (IndexError)".into());
                };
                ba.push(b0);
                bb.push(b1);
                h.push(half[j]);
                rh.push(road_half[j]);
                ws.push(nmax(w0, w1));
            }
            g += r.xy.len();
        }
        if any_w {
            for s in 0..ns {
                h[s] += ws[s];
                rh[s] += ws[s];
            }
        }
        let mut ridr: Vec<i64> = rows.iter().map(|r| {
            let v = rid[r.k];
            if v >= 0 { v } else { (1i64 << 40) + r.k as i64 }
        }).collect();
        // one road planned in pieces is one road in the blend (CONT_*): union of the pieces carrying straight on
        let mut items = Vec::new();
        for (r, row) in rows.iter().enumerate() {
            let rk = rank[row.k];
            if rk < 0 {
                continue;
            }
            let m = row.xy.len();
            for (e, (q, nb)) in [(row.xy[0], row.xy[1]), (row.xy[m - 1], row.xy[m - 2])].into_iter().enumerate() {
                let d = [q[0] - nb[0], q[1] - nb[1]];
                let ln = d[0].dhypot(d[1]);
                if ln > 1e-9 {
                    let zq = if e == 0 { row.z[0] } else { row.z[m - 1] };
                    items.push((q, (r, [d[0] / ln, d[1] / ln], zq, rk)));
                }
            }
        }
        let mut parent: Vec<usize> = (0..n).collect();
        fn find(parent: &mut [usize], mut i: usize) -> usize {
            while parent[i] != i {
                parent[i] = parent[parent[i]];
                i = parent[i];
            }
            i
        }
        for group in end_groups(items) {
            for (i, &(nn, d, zq, rk)) in group.iter().enumerate() {
                for &(m, d2, z2, rk2) in &group[i + 1..] {
                    if rk != rk2 || d[0] * d2[0] + d[1] * d2[1] > cont_cos_min || (zq - z2).abs() > cont_join {
                        continue;
                    }
                    let (x, y) = (find(&mut parent, nn), find(&mut parent, m));
                    if x != y {
                        parent[y] = x;
                    }
                }
            }
        }
        let mut members: HashMap<usize, Vec<usize>> = HashMap::new();
        for r in 0..n {
            let root = find(&mut parent, r);
            members.entry(root).or_default().push(r);
        }
        for (root, ms) in &members {
            if ms.len() > 1 {
                for &r in ms {
                    ridr[r] = (1i64 << 41) + *root as i64;
                }
            }
        }
        let mut ridv: Vec<i64> = Vec::with_capacity(ns);
        for r in 0..n {
            for _ in 0..nseg[r] {
                ridv.push(ridr[r]);
            }
        }
        // a pass road's legs between its hairpins' apexes count as roads of their own (roads.hairpin_apex_rows)
        for (r, row) in rows.iter().enumerate() {
            if steep[row.k] && nseg[r] >= 2 && row.xy.len() >= 3 {
                let v = numpy::ndarray::ArrayView2::from_shape((row.xy.len(), 2), row.xy.as_flattened()).unwrap();
                let m = crate::tight_bends_core(v, span_m, turn_deg);
                let mut apex = Vec::new();
                let mut i = 0;
                while i < m.len() {
                    if !m[i] {
                        i += 1;
                        continue;
                    }
                    let mut j = i;
                    while j + 1 < m.len() && m[j + 1] {
                        j += 1;
                    }
                    apex.push((i + j) / 2);
                    i = j + 1;
                }
                if !apex.is_empty() {
                    for s in 0..nseg[r] {
                        let leg = apex.iter().filter(|&&a| a <= s).count() as i64;
                        ridv[s0[r] + s] += leg << leg_shift;
                    }
                }
            }
        }
        // a plan's end where another's lies and the road carries straight on through it (LINK_TURN_DEG) goes on
        let mut items = Vec::new();
        for (r, row) in rows.iter().enumerate() {
            let m = row.xy.len();
            for (e, (q, nb)) in [(row.xy[0], row.xy[1]), (row.xy[m - 1], row.xy[m - 2])].into_iter().enumerate() {
                let d = [q[0] - nb[0], q[1] - nb[1]];
                let ln = d[0].dhypot(d[1]);
                if ln > 1e-9 {
                    items.push((q, (r, e, [d[0] / ln, d[1] / ln])));
                }
            }
        }
        let mut goes_on: Vec<Option<Vec<(usize, usize)>>> = vec![None; 2 * n];
        for group in end_groups(items) {
            if group.len() < 2 {
                continue;
            }
            for &(nn, e, d) in &group {
                let into: Vec<(usize, usize)> = group.iter()
                    .filter(|&&(m, _, d2)| m != nn && d[0] * d2[0] + d[1] * d2[1] <= link_cos_min)
                    .map(|&(m, e2, _)| (m, e2)).collect();
                if !into.is_empty() {
                    goes_on[2 * nn + e] = Some(into);
                }
            }
        }
        let flags: Vec<[bool; 2]> = rows.iter().map(|r| abut[r.k]).collect();
        let mut lens: Vec<f64> = vec![f64::NAN; n];
        let mut has_len = vec![false; n];
        let beyond = |nn: usize, e: usize, lens: &mut Vec<f64>, has_len: &mut Vec<bool>| -> f64 {
            let mut best = f64::INFINITY;
            let mut todo = BinaryHeap::new();
            todo.push(Step(0.0, nn, e));
            let mut done: HashSet<(usize, usize)> = HashSet::new();
            while let Some(Step(acc, a_, ea)) = todo.pop() {
                if acc >= best {
                    break;
                }
                if !done.insert((a_, ea)) {
                    continue;
                }
                if let Some(into) = &goes_on[2 * a_ + ea] {
                    for &(m, e2) in into {
                        if flags[m][e2] {
                            if acc < best {
                                best = acc;
                            }
                            continue;
                        }
                        if !has_len[m] {
                            let hy: Vec<f64> = rows[m].xy.windows(2)
                                .map(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1])).collect();
                            lens[m] = np_sum(&hy);
                            has_len[m] = true;
                        }
                        let far = acc + lens[m];
                        if far >= abut_near {
                            continue;
                        }
                        if flags[m][1 - e2] {
                            if far < best {
                                best = far;
                            }
                        } else {
                            todo.push(Step(far, m, 1 - e2));
                        }
                    }
                }
            }
            if best < abut_near { best } else { f64::NAN }
        };
        let mut ab = vec![0u8; ns];
        let mut abd = vec![f64::NAN; ns * 2];
        let mut cutp = vec![f64::NAN; ns * 6];
        for r in 0..n {
            let mut offs = [f64::NAN; 2];
            for (e, o) in offs.iter_mut().enumerate() {
                *o = if flags[r][e] { 0.0 } else if goes_on[2 * r + e].is_some() {
                    beyond(r, e, &mut lens, &mut has_len)
                } else { f64::NAN };
            }
            if (offs[0].is_finite() || offs[1].is_finite()) && nseg[r] > 0 {
                let s = arc(&rows[r].xy);
                let last = s[s.len() - 1];
                if offs[0].is_finite() {
                    for k in 0..nseg[r] {
                        let to = s[k] + offs[0];
                        let near = to < abut_near;
                        ab[s0[r] + k] |= near as u8;
                        abd[(s0[r] + k) * 2] = if near { to } else { f64::NAN };
                    }
                }
                if offs[1].is_finite() {
                    for k in 0..nseg[r] {
                        let to = last - s[k + 1] + offs[1];
                        let near = to < abut_near;
                        ab[s0[r] + k] |= near as u8;
                        abd[(s0[r] + k) * 2 + 1] = if near { to } else { f64::NAN };
                    }
                }
            }
            if let Some(c) = &rows[r].cut {
                // (numpy's assignment: as many rows as segments, or one row broadcast)
                if c.len() == nseg[r] {
                    for (k, row) in c.iter().enumerate() {
                        cutp[(s0[r] + k) * 6..(s0[r] + k + 1) * 6].copy_from_slice(row);
                    }
                } else if c.len() == 1 {
                    for k in 0..nseg[r] {
                        cutp[(s0[r] + k) * 6..(s0[r] + k + 1) * 6].copy_from_slice(&c[0]);
                    }
                } else {
                    return Err(format!("plan_pack: {} cut rows for {} segments", c.len(), nseg[r]));
                }
            }
        }
        let mut st = Vec::with_capacity(ns);
        let mut first = vec![false; ns];
        let mut lastv = vec![false; ns];
        for r in 0..n {
            for _ in 0..nseg[r] {
                st.push(steep[rows[r].k] as u8);
            }
            if nseg[r] > 0 {
                first[s0[r]] = true;
                lastv[s0[r + 1] - 1] = true;
            }
        }
        for r in 0..n {
            if goes_on[2 * r].is_some() {
                first[s0[r]] = false;
            }
            if goes_on[2 * r + 1].is_some() {
                lastv[s0[r + 1] - 1] = false;
            }
        }
        let lk: Vec<u8> = (0..ns).map(|s| (!first[s]) as u8 | (((!lastv[s]) as u8) << 1)).collect();
        let rk: Vec<i64> = (0..n).flat_map(|r| std::iter::repeat_n(rank[rows[r].k], nseg[r])).collect();
        let boxv: Vec<f64> = rows.iter().flat_map(|r| r.bbox).collect();
        let of: Vec<i64> = (0..n).flat_map(|r| std::iter::repeat_n(r as i64, nseg[r])).collect();
        let s0v: Vec<i64> = s0.iter().map(|&v| v as i64).collect();
        Ok(Some((n, a, b, za, zb, ba, bb, h, rh, ridv, rk, ab, abd, cutp, st, lk, boxv, of, s0v)))
    }
}

/// grading._Planner._plan_set's merge in one call (one reading of the plans): weld_continuations (when weld; its
/// failure, as an error message, leaves the plans as they were), trim_side_mouths (when trim) and PlanPack of the plans
/// that leaves, each as its own function above. The plans as plan_trim_mouths and plan_pack take them. Returns a dict:
/// weld_err (None or the message), welded (plans,) and wz (the welded heights, as plan_weld's), trimmed / ends / txy /
/// tz / toff (as plan_trim_mouths'), pack (plan_pack's dict).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn plan_merge<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                       z: PyReadonlyArray1<'py, f64>, bank: PyReadonlyArray1<'py, f64>,
                       bank_off: PyReadonlyArray1<'py, i64>, has_bank: PyReadonlyArray1<'py, bool>,
                       widen: PyReadonlyArray1<'py, f64>, widen_off: PyReadonlyArray1<'py, i64>,
                       has_widen: PyReadonlyArray1<'py, bool>, half: PyReadonlyArray1<'py, f64>,
                       road_half: PyReadonlyArray1<'py, f64>, rid: PyReadonlyArray1<'py, i64>,
                       rank: PyReadonlyArray1<'py, i64>, abut: PyReadonlyArray2<'py, bool>,
                       steep: PyReadonlyArray1<'py, bool>, bbox: PyReadonlyArray2<'py, f64>,
                       cut: PyReadonlyArray2<'py, f64>, cut_off: PyReadonlyArray1<'py, i64>,
                       has_cut: PyReadonlyArray1<'py, bool>, wmax: PyReadonlyArray1<'py, f64>, weld: bool, trim: bool,
                       join_m: f64, max_step: f64, taper: f64, reach: f64, ramp_m: f64, dupe_m: f64,
                       cont_cos_min: f64, cont_join: f64, link_cos_min: f64, abut_near: f64, t_max: f64,
                       leg_shift: u32, span_m: f64, turn_deg: f64) -> PyResult<Bound<'py, PyDict>> {
    let p = Plans::read(&xy, &off, &z, "plan_merge")?;
    let n = p.n();
    let mut inp = read_pack_in(n, &bank, &bank_off, &has_bank, &widen, &widen_off, &has_widen, &half, &road_half, &rid,
                               &rank, &abut, &steep, &bbox, &cut, &cut_off, &has_cut)?;
    let wm = wmax.as_array();
    if wm.len() != n {
        return Err(PyValueError::new_err("plan_merge: mismatched plan arrays"));
    }
    // (the most each plan widens, None: no widen: grading.set_widen's box)
    let wmax: Vec<Option<f64>> = (0..n).map(|j| match &inp.widens[j] {
        Some(w) if !w.is_empty() => Some(wm[j]),
        _ => None,
    }).collect();
    let k = PackConsts { dupe_m, cont_cos_min, cont_join, link_cos_min, abut_near, t_max, leg_shift, span_m, turn_deg };
    let res = py.detach(|| -> Result<_, String> {
        let mut p = p;
        let mut weld_err = None;
        let mut welded = vec![false; n];
        if weld {
            match weld_core(&p, &inp.abut, &inp.rank, join_m, max_step, taper) {
                Ok((zout, changed)) => {
                    p.z = zout;
                    for j in 0..n {
                        if changed[j] {
                            // (a welded plan is a new Plan: its box from its line, set_widen's)
                            inp.bbox[j] = plan_bbox(p.xy(j), inp.half[j], wmax[j], t_max);
                        }
                    }
                    welded = changed;
                }
                Err(e) => weld_err = Some(e),
            }
        }
        let wz = p.z.clone();
        let (mut out_i, mut out_e) = (Vec::new(), Vec::new());
        let mut q = None;
        if trim {
            let (oi, oe, over) = trim_core(&p, &inp.half, &inp.road_half, &inp.abut, &inp.rank, &mut inp.bbox,
                                           &inp.widens, reach, ramp_m, t_max);
            if !oi.is_empty() {
                // (the trimmed plans' lines in the plans packed; their cuttings dropped)
                let mut xy2 = Vec::with_capacity(p.xy.len());
                let mut z2 = Vec::with_capacity(p.z.len());
                let mut off2 = vec![0usize];
                for j in 0..n {
                    match &over[j] {
                        Some((a, b, at)) => {
                            xy2.extend_from_slice(a);
                            z2.extend_from_slice(b);
                            inp.cuts[j] = None;
                            // (its bank and widen cut with its line: a longer one read the next plans' vertices'
                            // values in the pack, 289 of 905 banked plans in 87 captured sets, 2026-11-11)
                            if let Some(v) = inp.banks[j].as_ref().and_then(|v| trim_vals(v, at)) {
                                inp.banks[j] = Some(v);
                            }
                            if let Some(v) = inp.widens[j].as_ref().and_then(|v| trim_vals(v, at)) {
                                inp.widens[j] = Some(v);
                            }
                        }
                        None => {
                            xy2.extend_from_slice(p.xy(j));
                            z2.extend_from_slice(p.z(j));
                        }
                    }
                    off2.push(xy2.len());
                }
                q = Some((Plans { xy: xy2, z: z2, off: off2 }, over));
            }
            out_i = oi;
            out_e = oe;
        }
        let (packed, over) = match q {
            Some((p2, over)) => (pack_core(&p2, &inp, &k)?, Some(over)),
            None => (pack_core(&p, &inp, &k)?, None),
        };
        let (mut txy, mut tz, mut toff) = (Vec::new(), Vec::new(), vec![0i64]);
        let (mut tbank, mut twiden) = (Vec::new(), Vec::new());
        if let Some(over) = over {
            for &i in &out_i {
                let (a, b, _) = over[i].as_ref().unwrap();
                txy.extend(a.iter().flat_map(|q| [q[0], q[1]]));
                tz.extend_from_slice(b);
                toff.push(tz.len() as i64);
                tbank.push(inp.banks[i].clone());
                twiden.push(inp.widens[i].clone());
            }
        }
        Ok((weld_err, welded, wz, out_i, out_e, txy, tz, toff, tbank, twiden, packed))
    }).map_err(PyValueError::new_err)?;
    let (weld_err, welded, wz, out_i, out_e, txy, tz, toff, tbank, twiden, packed) = res;
    let d = PyDict::new(py);
    d.set_item("weld_err", weld_err)?;
    d.set_item("welded", welded.into_pyarray(py))?;
    d.set_item("wz", wz.into_pyarray(py))?;
    d.set_item("trimmed", out_i)?;
    d.set_item("ends", out_e)?;
    let kk = tz.len();
    d.set_item("txy", Array2::from_shape_vec((kk, 2), txy).unwrap().into_pyarray(py))?;
    d.set_item("tz", tz.into_pyarray(py))?;
    d.set_item("toff", toff.into_pyarray(py))?;
    // (the trimmed plans' banks and widens, cut with their lines; None: none)
    let tb: Vec<Option<Bound<'_, PyArray1<f64>>>> = tbank.into_iter().map(|v| v.map(|v| v.into_pyarray(py))).collect();
    let tw: Vec<Option<Bound<'_, PyArray1<f64>>>> = twiden.into_iter().map(|v| v.map(|v| v.into_pyarray(py))).collect();
    d.set_item("tbank", tb)?;
    d.set_item("twiden", tw)?;
    d.set_item("pack", pack_dict(py, packed)?)?;
    Ok(d)
}

/// PlanPack's attributes as a dict.
fn pack_dict(py: Python<'_>, res: PackOut) -> PyResult<Bound<'_, PyDict>> {
    let d = PyDict::new(py);
    match res {
        None => d.set_item("n", 0)?,
        Some((n, a, b, za, zb, ba, bb, h, rh, ridv, rk, ab, abd, cutp, st, lk, boxv, of, s0v)) => {
            let ns = za.len();
            d.set_item("n", n)?;
            d.set_item("a", Array2::from_shape_vec((ns, 2), a).unwrap().into_pyarray(py))?;
            d.set_item("b", Array2::from_shape_vec((ns, 2), b).unwrap().into_pyarray(py))?;
            d.set_item("za", za.into_pyarray(py))?;
            d.set_item("zb", zb.into_pyarray(py))?;
            d.set_item("ba", ba.into_pyarray(py))?;
            d.set_item("bb", bb.into_pyarray(py))?;
            d.set_item("h", h.into_pyarray(py))?;
            d.set_item("rh", rh.into_pyarray(py))?;
            d.set_item("rid", ridv.into_pyarray(py))?;
            d.set_item("rk", rk.into_pyarray(py))?;
            d.set_item("ab", ab.into_pyarray(py))?;
            d.set_item("abd", Array2::from_shape_vec((ns, 2), abd).unwrap().into_pyarray(py))?;
            d.set_item("cut", Array2::from_shape_vec((ns, 6), cutp).unwrap().into_pyarray(py))?;
            d.set_item("st", st.into_pyarray(py))?;
            d.set_item("lk", lk.into_pyarray(py))?;
            d.set_item("box", Array2::from_shape_vec((n, 4), boxv).unwrap().into_pyarray(py))?;
            d.set_item("of", of.into_pyarray(py))?;
            d.set_item("s0", s0v.into_pyarray(py))?;
        }
    }
    Ok(d)
}

/// PlanPack.near: the segment arrays (a, b, za, zb, h, rh, rid, rk, ab, cut, ba, bb, st, lk, abd, as PlanPack keeps
/// them) of the plans whose boxes (box (plans, 4), their segments s0[j]..s0[j + 1]) meet (x0, y0, x1, y1). None when
/// none does, True when all do (the caller hands its own arrays), else a tuple of new arrays.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn plan_pack_near<'py>(py: Python<'py>, bx: PyReadonlyArray2<'py, f64>, s0: PyReadonlyArray1<'py, i64>, x0: f64,
                           y0: f64, x1: f64, y1: f64, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>,
                           za: PyReadonlyArray1<'py, f64>, zb: PyReadonlyArray1<'py, f64>,
                           h: PyReadonlyArray1<'py, f64>, rh: PyReadonlyArray1<'py, f64>,
                           rid: PyReadonlyArray1<'py, i64>, rk: PyReadonlyArray1<'py, i64>,
                           ab: PyReadonlyArray1<'py, u8>, cut: PyReadonlyArray2<'py, f64>,
                           ba: PyReadonlyArray1<'py, f64>, bb: PyReadonlyArray1<'py, f64>,
                           st: PyReadonlyArray1<'py, u8>, lk: PyReadonlyArray1<'py, u8>,
                           abd: PyReadonlyArray2<'py, f64>) -> PyResult<Py<PyAny>> {
    let (bxa, s0a) = (bx.as_array(), s0.as_array());
    let n = bxa.nrows();
    if s0a.len() != n + 1 || bxa.ncols() != 4 {
        return Err(PyValueError::new_err("plan_pack_near: mismatched pack arrays"));
    }
    let mut runs: Vec<(usize, usize)> = Vec::new();
    let mut all = true;
    for j in 0..n {
        let keep = !(bxa[[j, 2]] < x0 || bxa[[j, 0]] > x1 || bxa[[j, 3]] < y0 || bxa[[j, 1]] > y1);
        if !keep {
            all = false;
            continue;
        }
        let (s, e) = (s0a[j] as usize, s0a[j + 1] as usize);
        match runs.last_mut() {
            Some(r) if r.1 == s => r.1 = e,
            _ => runs.push((s, e)),
        }
    }
    if runs.is_empty() {
        return Ok(py.None());
    }
    if all {
        return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
    }
    let ns = s0a[n] as usize;
    let k: usize = runs.iter().map(|r| r.1 - r.0).sum();
    fn take1<'py, T: numpy::Element + Copy>(py: Python<'py>, v: &PyReadonlyArray1<'py, T>, runs: &[(usize, usize)],
                                            k: usize, ns: usize) -> PyResult<Bound<'py, PyAny>> {
        let s = v.as_slice().map_err(|_| PyValueError::new_err("plan_pack_near: a pack array not contiguous"))?;
        if s.len() != ns {
            return Err(PyValueError::new_err("plan_pack_near: mismatched pack arrays"));
        }
        let mut o = Vec::with_capacity(k);
        for &(a, b) in runs {
            o.extend_from_slice(&s[a..b]);
        }
        Ok(o.into_pyarray(py).into_any())
    }
    fn take2<'py>(py: Python<'py>, v: &PyReadonlyArray2<'py, f64>, runs: &[(usize, usize)], k: usize, ns: usize)
                  -> PyResult<Bound<'py, PyAny>> {
        let c = v.as_array().ncols();
        let s = v.as_slice().map_err(|_| PyValueError::new_err("plan_pack_near: a pack array not contiguous"))?;
        if v.as_array().nrows() != ns {
            return Err(PyValueError::new_err("plan_pack_near: mismatched pack arrays"));
        }
        let mut o = Vec::with_capacity(k * c);
        for &(a, b) in runs {
            o.extend_from_slice(&s[a * c..b * c]);
        }
        Ok(Array2::from_shape_vec((k, c), o).unwrap().into_pyarray(py).into_any())
    }
    let items = vec![take2(py, &a, &runs, k, ns)?, take2(py, &b, &runs, k, ns)?, take1(py, &za, &runs, k, ns)?,
                     take1(py, &zb, &runs, k, ns)?, take1(py, &h, &runs, k, ns)?, take1(py, &rh, &runs, k, ns)?,
                     take1(py, &rid, &runs, k, ns)?, take1(py, &rk, &runs, k, ns)?, take1(py, &ab, &runs, k, ns)?,
                     take2(py, &cut, &runs, k, ns)?, take1(py, &ba, &runs, k, ns)?, take1(py, &bb, &runs, k, ns)?,
                     take1(py, &st, &runs, k, ns)?, take1(py, &lk, &runs, k, ns)?, take2(py, &abd, &runs, k, ns)?];
    Ok(PyTuple::new(py, items)?.into_any().unbind())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round1_key_matches_python() {
        // (x, round(x, 1) * 10) from CPython
        let cases = [(0.15, 1), (0.25, 2), (0.35, 3), (-0.25, -2), (-0.15, -1), (2.675, 27), (0.05, 1), (-0.05, -1),
                     (1234.25, 12342), (-1234.25, -12342), (1234.35, 12343), (99999.95, 999999), (-99999.95, -999999),
                     (0.45, 5), (1e-20, 0), (-1e-20, 0), (0.25000000000000006, 3), (4916.067795588078, 49161),
                     (9671.479570429176, 96715), (16892.99986661668, 168930), (-18839.79086865541, -188398),
                     (-1375.0938248757848, -13751), (5958.982125476967, 59590), (16036.07, 160361),
                     (-15471.710000000001, -154717), (-1237.19, -12372), (-10137.04, -101370), (1750.48, 17505)];
        for (x, k) in cases {
            assert_eq!(round1_key(x), Some(k), "round({x}, 1)");
        }
        assert_eq!(round1_key(f64::NAN), None);
    }

    #[test]
    fn nearest_takes_the_first_minimum() {
        let xy = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]];
        let z = [1.0, 2.0, 4.0];
        assert_eq!(nearest(&xy, &z, 5.0, 1.0), (1.0, 1.5, 0, 0.5));
        // (the shared vertex: equally near both segments, the first one's)
        let (d, _, i, _) = nearest(&xy, &z, 11.0, -1.0);
        assert_eq!(i, 0);
        assert!((d - 2f64.sqrt()).abs() < 1e-12);
    }
}
