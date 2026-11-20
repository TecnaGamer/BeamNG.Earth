//! railways.profile (version 525, the full port's r2a20): per rail part, its bed heights from the ground under it, the
//! bridge decks' approach levels, the road tunnels' clearance under the decks and the decks' heights run out along the
//! track at GRADE (two Dijkstra runs over the parts' chain graph). The Python laid each railprofile lattice node's
//! window part by part in numpy (~1.2 % of a cold road-tile build's busy CPU, the r2a19 profile). The ground stays the
//! caller's (a Python function, its memo and its reads): the glue asks it for the parts' points, the approach rays
//! (rail_approach_rays) and the tunnels' mouths in the Python's order and hands the heights in. Every sum, product and
//! comparison in the Python's order: numpy's maximum / minimum / clip ties and NaNs, its pairwise mean, its median,
//! Python's max() and heapq's order (bit for bit, no FMA, no libm but the kernels' hypot).
use crate::chaingraph::EndJoin;
use crate::dm::Dm;
use crate::railbeds::bed_heights;

use std::collections::BTreeMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// np.maximum(a, b): a NaN either side wins, else a when a > b, else b (numpy's tie: b).
fn np_max(a: f64, b: f64) -> f64 {
    if a.is_nan() { a } else if b.is_nan() { b } else if a > b { a } else { b }
}
/// np.minimum(a, b): a NaN either side wins, else a when a < b, else b.
fn np_min(a: f64, b: f64) -> f64 {
    if a.is_nan() { a } else if b.is_nan() { b } else if a < b { a } else { b }
}
/// np.clip(x, lo, hi) for floats: x on a tie with lo, NaN through.
fn np_clip(x: f64, lo: f64, hi: f64) -> f64 {
    if x.is_nan() {
        return x;
    }
    let v = if lo > x { lo } else { x };
    if hi < v { hi } else { v }
}
/// Python's max(a, b): a unless b > a.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// numpy's pairwise sum of a float64 line (add.reduce from 0.0: fewer than 8 values in order, else 8 partial sums ...).
fn pairwise(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut r = 0.0f64;
        for &x in a {
            r += x;
        }
        r
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
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
        pairwise(&a[..n2]) + pairwise(&a[n2..])
    }
}

/// np.mean of a non-empty float64 line.
fn np_mean(a: &[f64]) -> f64 {
    pairwise(a) / a.len() as f64
}

/// np.median of finite values: the middle one (or the mean of the middle two) of the sorted values.
fn np_median(seg: &[f64]) -> f64 {
    let mut v = seg.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let h = v.len() / 2;
    if v.len() % 2 == 1 { np_mean(&v[h..h + 1]) } else { np_mean(&v[h - 1..h + 1]) }
}

/// railways._envelope(s, req, grade): the lowest line over every req climbing at most grade (forward, then backward).
fn envelope(s: &[f64], req: &[f64], grade: f64) -> Vec<f64> {
    let mut out = req.to_vec();
    let m = s.len().min(out.len());
    let rise: Vec<f64> = (1..m).map(|k| grade * (s[k] - s[k - 1])).collect();
    for k in 1..=rise.len() {
        let v = out[k - 1] - rise[k - 1];
        if v > out[k] {
            out[k] = v;
        }
    }
    for k in (0..rise.len()).rev() {
        let v = out[k + 1] - rise[k];
        if v > out[k] {
            out[k] = v;
        }
    }
    out
}

/// heapq's (distance, node) tuple order: the distances by ==, then the nodes.
fn lt(a: (f64, usize), b: (f64, usize)) -> bool {
    if a.0 == b.0 { a.1 < b.1 } else { a.0 < b.0 }
}

fn sift_down(h: &mut [(f64, usize)], start: usize, mut pos: usize) {
    let item = h[pos];
    while pos > start {
        let parent = (pos - 1) >> 1;
        if lt(item, h[parent]) {
            h[pos] = h[parent];
            pos = parent;
            continue;
        }
        break;
    }
    h[pos] = item;
}

fn sift_up(h: &mut [(f64, usize)], mut pos: usize) {
    let end = h.len();
    let start = pos;
    let item = h[pos];
    let mut child = 2 * pos + 1;
    while child < end {
        let right = child + 1;
        if right < end && !lt(h[child], h[right]) {
            child = right;
        }
        h[pos] = h[child];
        pos = child;
        child = 2 * pos + 1;
    }
    h[pos] = item;
    sift_down(h, start, pos);
}

/// roads._dijkstra(adj, seeds) with heapq's own heap (the seeds in their dict's order).
fn dijkstra(adj: &[Vec<(usize, f64)>], seeds: &[(usize, f64)]) -> Vec<f64> {
    let mut d = vec![f64::INFINITY; adj.len()];
    let mut q: Vec<(f64, usize)> = Vec::new();
    for &(n, d0) in seeds {
        if d0 < d[n] {
            d[n] = d0;
            q.push((d0, n));
        }
    }
    for i in (0..q.len() / 2).rev() {
        sift_up(&mut q, i);
    }
    while let Some(last) = q.pop() {
        let (dd, u) = if q.is_empty() {
            last
        } else {
            let top = q[0];
            q[0] = last;
            sift_up(&mut q, 0);
            top
        };
        if dd > d[u] {
            continue;
        }
        for &(v, w) in &adj[u] {
            let nd = dd + w;
            if nd < d[v] {
                d[v] = nd;
                q.push((nd, v));
                let at = q.len() - 1;
                sift_down(&mut q, 0, at);
            }
        }
    }
    d
}

/// Python's max() over values in order (the first of equals; a NaN first stays).
fn py_max_of(v: &[(usize, f64)]) -> f64 {
    let mut cur = v[0].1;
    for &(_, x) in &v[1..] {
        if x > cur {
            cur = x;
        }
    }
    cur
}

/// The parts end to end: points (N, 2) and offsets (P + 1).
struct Parts {
    pts: Vec<[f64; 2]>,
    off: Vec<usize>,
}

impl Parts {
    fn of(xy: &PyReadonlyArray2<'_, f64>, off: &PyReadonlyArray1<'_, i64>, what: &str) -> PyResult<Parts> {
        let v = xy.as_array();
        if v.ncols() != 2 {
            return Err(PyValueError::new_err(format!("{what}: the parts' points must be (n, 2)")));
        }
        let pts: Vec<[f64; 2]> = v.rows().into_iter().map(|r| [r[0], r[1]]).collect();
        let o: Vec<i64> = off.as_array().iter().copied().collect();
        if o.is_empty() || o[0] != 0 || *o.last().unwrap() as usize != pts.len() || o.windows(2).any(|w| w[1] <= w[0]) {
            return Err(PyValueError::new_err(format!("{what}: inconsistent part offsets (a part of no points?)")));
        }
        Ok(Parts { pts, off: o.iter().map(|&x| x as usize).collect() })
    }
    fn n(&self) -> usize {
        self.off.len() - 1
    }
    fn xy(&self, p: usize) -> &[[f64; 2]] {
        &self.pts[self.off[p]..self.off[p + 1]]
    }
}

/// The approach rays of one deck part xy: per end (start, end), the unit direction on along its last segment, or None
/// (fewer than 2 points, a segment shorter than 1e-6 m). railways.approach_levels_many's rays.
fn ray_dirs(xy: &[[f64; 2]]) -> [Option<([f64; 2], [f64; 2])>; 2] {
    if xy.len() < 2 {
        return [None, None];
    }
    let k = xy.len();
    let one = |end: [f64; 2], nxt: [f64; 2]| {
        let u = [end[0] - nxt[0], end[1] - nxt[1]];
        let n = u[0].dhypot(u[1]);
        if n > 1e-6 { Some((end, [u[0] / n, u[1] / n])) } else { None }
    };
    [one(xy[0], xy[1]), one(xy[k - 1], xy[k - 2])]
}

const GROUND: u8 = 0;
const BRIDGE: u8 = 1;
const TUNNEL: u8 = 2;

/// A part's kind ("ground", "bridge", "tunnel"; anything else, a str or not, is none of them).
fn kind_of(k: &Bound<'_, PyAny>) -> u8 {
    match k.extract::<String>().as_deref() {
        Ok("ground") => GROUND,
        Ok("bridge") => BRIDGE,
        Ok("tunnel") => TUNNEL,
        _ => 3,
    }
}

/// railways.approach_levels_many's ground points: every bridge part's (kinds) two rays (end + dir * d), end to end, as
/// (m, 2), the rays of no direction left out.
#[pyfunction]
#[pyo3(signature = (xy, off, kinds, d))]
pub fn rail_approach_rays<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                               kinds: Vec<Bound<'py, PyAny>>, d: PyReadonlyArray1<'py, f64>)
                               -> PyResult<Bound<'py, PyArray2<f64>>> {
    let parts = Parts::of(&xy, &off, "rail_approach_rays")?;
    if kinds.len() != parts.n() {
        return Err(PyValueError::new_err("rail_approach_rays: one kind per part"));
    }
    let dv: Vec<f64> = d.as_array().iter().copied().collect();
    let mut out: Vec<f64> = Vec::new();
    for (p, k) in kinds.iter().enumerate() {
        if kind_of(k) != BRIDGE {
            continue;
        }
        for (end, u) in ray_dirs(parts.xy(p)).into_iter().flatten() {
            for &di in &dv {
                out.push(end[0] + u[0] * di);
                out.push(end[1] + u[1] * di);
            }
        }
    }
    let rows = out.len() / 2;
    Ok(numpy::ndarray::Array2::from_shape_vec((rows, 2), out).expect("rays").into_pyarray(py))
}

/// The constants of railways.profile, in the glue's order.
struct Consts {
    step: f64,
    grade: f64,
    smooth_r: usize,
    from_i: usize,
    flat_w: usize,
    flat_m: f64,
    tol: f64,
    amin: f64,
    araise: f64,
    uclear: f64,
    deck_d: f64,
    ballast_h: f64,
    deck_raise: f64,
    seed_lift: f64,
}

/// railways.profile of the parts (points xy end to end, off; kinds and classes per part): (the beds, one array per
/// part; per part whether its bed IS the ground the glue handed in (a ground part of fewer than 3 points no level or
/// deck lifted: the Python returned that very array)). zg: the ground at every point; base: the ground parts' heights
/// before the level lifts (railxing's shared corridor ground, or zg); ray_z: the ground along rail_approach_rays' rays;
/// d: the rays' distances; (ti, pi, ki, fr): rail_underpass_hits of the parts and the underpasses; mouth_z: the
/// ground at the first and last point of each underpass that has hits (ascending), end to end. counts: (the bed's
/// smoothing half width, the first approach step, the level stretch's steps); consts: STEP_M, GRADE, APPROACH_FLAT_M,
/// APPROACH_TOL_M, APPROACH_MIN_M, APPROACH_RAISE_MAX_M, UNDERPASS_CLEAR_M, DECK_D, BALLAST_H, DECK_RAISE_MAX_M,
/// SEED_LIFT_MAX_M.
#[pyfunction]
#[pyo3(signature = (xy, off, kinds, classes, line_grade, zg, base, ray_z, d, ti, pi, ki, fr, mouth_z, counts, consts))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn rail_profile<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                         kinds: Vec<Bound<'py, PyAny>>, classes: Vec<Bound<'py, PyAny>>,
                         line_grade: BTreeMap<String, f64>,
                         zg: PyReadonlyArray1<'py, f64>, base: PyReadonlyArray1<'py, f64>,
                         ray_z: PyReadonlyArray1<'py, f64>, d: PyReadonlyArray1<'py, f64>,
                         ti: PyReadonlyArray1<'py, i64>, pi: PyReadonlyArray1<'py, i64>, ki: PyReadonlyArray1<'py, i64>,
                         fr: PyReadonlyArray1<'py, f64>, mouth_z: PyReadonlyArray1<'py, f64>,
                         counts: (usize, usize, usize), consts: Vec<f64>)
                         -> PyResult<(Vec<Bound<'py, PyArray1<f64>>>, Vec<bool>)> {
    let parts = Parts::of(&xy, &off, "rail_profile")?;
    let np_ = parts.n();
    if kinds.len() != np_ || classes.len() != np_ {
        return Err(PyValueError::new_err("rail_profile: one kind and class per part"));
    }
    let n_pts = parts.pts.len();
    let zg: Vec<f64> = zg.as_array().iter().copied().collect();
    let base: Vec<f64> = base.as_array().iter().copied().collect();
    if zg.len() != n_pts || base.len() != n_pts {
        return Err(PyValueError::new_err("rail_profile: a ground height per point"));
    }
    let ray_z: Vec<f64> = ray_z.as_array().iter().copied().collect();
    let dv: Vec<f64> = d.as_array().iter().copied().collect();
    let ti: Vec<i64> = ti.as_array().iter().copied().collect();
    let pi: Vec<i64> = pi.as_array().iter().copied().collect();
    let ki: Vec<i64> = ki.as_array().iter().copied().collect();
    let fr: Vec<f64> = fr.as_array().iter().copied().collect();
    let mz: Vec<f64> = mouth_z.as_array().iter().copied().collect();
    if pi.len() != ti.len() || ki.len() != ti.len() || fr.len() != ti.len() {
        return Err(PyValueError::new_err("rail_profile: the underpass hits' columns differ in length"));
    }
    if consts.len() != 11 {
        return Err(PyValueError::new_err("rail_profile: 11 constants"));
    }
    let c = Consts {
        smooth_r: counts.0, from_i: counts.1, flat_w: counts.2, step: consts[0], grade: consts[1], flat_m: consts[2],
        tol: consts[3], amin: consts[4], araise: consts[5], uclear: consts[6], deck_d: consts[7], ballast_h: consts[8],
        deck_raise: consts[9], seed_lift: consts[10],
    };
    let kc: Vec<u8> = kinds.iter().map(kind_of).collect();
    // (LINE_GRADE.get(class, GRADE): a class that is no str has no grade of its own)
    let grades: Vec<f64> = classes.iter()
        .map(|cl| cl.extract::<String>().ok().and_then(|k| line_grade.get(&k).copied()).unwrap_or(c.grade))
        .collect();
    let got = py.detach(|| profile(&parts, &kc, &grades, &zg, &base, &ray_z, &dv, (&ti, &pi, &ki, &fr), &mz, &c));
    let (zb, alias) = got.map_err(PyValueError::new_err)?;
    Ok((zb.into_iter().map(|z| z.into_pyarray(py)).collect(), alias))
}

#[allow(clippy::too_many_arguments)]
fn profile(parts: &Parts, kinds: &[u8], grades: &[f64], zg_all: &[f64], base: &[f64], ray_z: &[f64], d: &[f64],
           hits: (&[i64], &[i64], &[i64], &[f64]), mouth_z: &[f64], c: &Consts)
           -> Result<(Vec<Vec<f64>>, Vec<bool>), String> {
    let np_ = parts.n();
    let zgs = |n: usize| &zg_all[parts.off[n]..parts.off[n + 1]];
    // roads._chain_graph: nodes (ends joined within 0.5 m), edges (a, b, the arc), adjacency (in the edges' order)
    let mut join = EndJoin::new(0.5);
    let mut n_nodes = 0usize;
    let mut edges: Vec<(usize, usize, Vec<f64>)> = Vec::with_capacity(np_);
    for p in 0..np_ {
        let xy = parts.xy(p);
        let mut s = Vec::with_capacity(xy.len());
        let mut acc = 0.0f64;
        s.push(0.0);
        for i in 1..xy.len() {
            acc += (xy[i][0] - xy[i - 1][0]).dhypot(xy[i][1] - xy[i - 1][1]);
            s.push(acc);
        }
        let mut ab = [0usize; 2];
        for (e, at) in [0usize, xy.len() - 1].into_iter().enumerate() {
            let (nd, new) = join.join(xy[at][0], xy[at][1]);
            if new {
                n_nodes += 1;
            }
            ab[e] = nd;
        }
        edges.push((ab[0], ab[1], s));
    }
    let mut adj: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n_nodes];
    for (i, j, s) in &edges {
        let w = s[s.len() - 1];
        adj[*i].push((*j, w));
        adj[*j].push((*i, w));
    }
    // the decks' approach levels: node -> (level, reach), the higher level of the ends meeting there
    let mut level: Vec<Option<(f64, f64)>> = vec![None; n_nodes];
    let nd = d.len();
    let mut k = 0usize;
    for p in 0..np_ {
        if kinds[p] != BRIDGE {
            continue;
        }
        let grade = grades[p];
        let rays = ray_dirs(parts.xy(p));
        let ends = [edges[p].0, edges[p].1];
        for (e, ray) in rays.iter().enumerate() {
            if ray.is_none() {
                continue;
            }
            if k + nd > ray_z.len() {
                return Err("rail_profile: fewer ray heights than rays".into());
            }
            let z = &ray_z[k..k + nd];
            k += nd;
            let mut got: Option<(f64, f64)> = None;
            if z.iter().all(|v| v.is_finite()) {
                let lim = grade * c.flat_m + c.tol;
                let mut i = c.from_i;
                while i + c.flat_w < nd {
                    let seg = &z[i..i + c.flat_w + 1];
                    let (mut lo, mut hi) = (seg[0], seg[0]);
                    for &v in &seg[1..] {
                        if v < lo {
                            lo = v;
                        }
                        if v > hi {
                            hi = v;
                        }
                    }
                    if hi - lo <= lim {
                        let lv = np_median(seg);
                        let rise = lv - z[0];
                        if c.amin <= rise && rise <= c.araise {
                            got = Some((lv, d[i]));
                        }
                        break;
                    }
                    i += 1;
                }
            }
            if let Some(g) = got {
                let node = ends[e];
                if g.0 > level[node].map_or(f64::NEG_INFINITY, |l| l.0) {
                    level[node] = Some(g);
                }
            }
        }
    }
    if k != ray_z.len() {
        return Err("rail_profile: more ray heights than rays".into());
    }
    // the beds: the ground parts' lifted to the levels and smoothed (bed_heights), the others the ground
    let mut zb: Vec<Vec<f64>> = Vec::with_capacity(np_);
    let mut alias = vec![false; np_];
    for p in 0..np_ {
        let zg = zgs(p);
        if kinds[p] != GROUND {
            zb.push(zg.to_vec());
            continue;
        }
        let (a, b, s) = (&edges[p].0, &edges[p].1, &edges[p].2);
        let mut lifted = base[parts.off[p]..parts.off[p + 1]].to_vec();
        let mut lifted_any = false;
        let last = s[s.len() - 1];
        for (e, node) in [*a, *b].into_iter().enumerate() {
            if let Some((lv, reach)) = level[node] {
                lifted_any = true;
                let lim = reach + c.step;
                for (i, v) in lifted.iter_mut().enumerate() {
                    let dist = if e == 0 { s[i] } else { last - s[i] };
                    if dist <= lim {
                        *v = np_max(*v, lv);
                    }
                }
            }
        }
        if lifted.len() < 3 {
            alias[p] = !lifted_any;
            zb.push(lifted);
        } else {
            zb.push(bed_heights(&lifted, c.smooth_r));
        }
    }
    // the underpasses with hits (ascending), each {part: its run of (point, fraction)}: the last run of a pair wins
    let (ti, pi, ki, fr) = hits;
    let mut cross: Vec<usize> = Vec::new();
    let mut runs: BTreeMap<(usize, usize), (usize, usize)> = BTreeMap::new();
    let mut a0 = 0usize;
    while a0 < ti.len() {
        let mut b0 = a0 + 1;
        while b0 < ti.len() && ti[b0] == ti[a0] && pi[b0] == pi[a0] {
            b0 += 1;
        }
        if ti[a0] < 0 || pi[a0] < 0 || pi[a0] as usize >= np_ {
            return Err("rail_profile: an underpass hit out of range".into());
        }
        runs.insert((ti[a0] as usize, pi[a0] as usize), (a0, b0));
        a0 = b0;
    }
    for &(t, _) in runs.keys() {
        if cross.last() != Some(&t) {
            cross.push(t);
        }
    }
    if mouth_z.len() != 2 * cross.len() {
        return Err("rail_profile: two mouth heights per underpass with hits".into());
    }
    // the ground parts' beds at each node (their ends)
    let mut ends: Vec<Vec<f64>> = vec![Vec::new(); n_nodes];
    for p in 0..np_ {
        if kinds[p] == GROUND {
            let z = &zb[p];
            ends[edges[p].0].push(z[0]);
            ends[edges[p].1].push(z[z.len() - 1]);
        }
    }
    let end_z = |node: usize, or: f64| if ends[node].is_empty() { or } else { np_mean(&ends[node]) };
    // tunnels straight between their portals; decks between their ends, over the ground, clear of the roads under
    let mut seeds: Vec<(usize, f64)> = Vec::new();
    let mut seed_at: Vec<Option<usize>> = vec![None; n_nodes];
    for p in 0..np_ {
        let kind = kinds[p];
        if kind != TUNNEL && kind != BRIDGE {
            continue;
        }
        let (a, b) = (edges[p].0, edges[p].1);
        let s = &edges[p].2;
        let g = zgs(p);
        let za = end_z(a, g[0]);
        let zb_ = end_z(b, g[g.len() - 1]);
        let last = s[s.len() - 1];
        if kind == TUNNEL {
            let l = py_max(last, 1e-9);
            zb[p] = s.iter().map(|&si| za + ((zb_ - za) * si) / l).collect();
            continue;
        }
        let za = if let Some((lv, _)) = level[a] { py_max(za, lv) } else { za };
        let zb_ = if let Some((lv, _)) = level[b] { py_max(zb_, lv) } else { zb_ };
        // deck_heights
        let l = py_max(last, 1e-9);
        let req: Vec<f64> = s.iter().zip(g).map(|(&si, &gi)| {
            let chord = za + ((zb_ - za) * si) / l;
            np_min(np_max(chord, gi), chord + c.deck_raise)
        }).collect();
        let mut z = envelope(s, &req, c.grade);
        for (ci, &t) in cross.iter().enumerate() {
            if let Some(&(r0, r1)) = runs.get(&(t, p)) {
                let (z0, z1) = (mouth_z[2 * ci], mouth_z[2 * ci + 1]);
                let mut req = z.clone();
                let mut vals = Vec::with_capacity(r1 - r0);
                for j in r0..r1 {
                    let i = ki[j];
                    if i < 0 || i as usize >= z.len() {
                        return Err("rail_profile: an underpass hit's point out of range".into());
                    }
                    let need = (((z0 + (z1 - z0) * fr[j]) + c.uclear) + c.deck_d) - c.ballast_h;
                    vals.push((i as usize, np_max(z[i as usize], need)));
                }
                for (i, v) in vals {
                    req[i] = v;
                }
                z = envelope(s, &req, c.grade);
            }
        }
        for (node, h) in [(a, z[0]), (b, z[z.len() - 1])] {
            match seed_at[node] {
                Some(at) => seeds[at].1 = py_max(seeds[at].1, h),
                None => {
                    seed_at[node] = Some(seeds.len());
                    seeds.push((node, py_max(f64::NEG_INFINITY, h)));
                }
            }
        }
        zb[p] = z;
    }
    if !seeds.is_empty() {
        let grade = c.grade;
        let top = py_max_of(&seeds);
        let dd = dijkstra(&adj, &seeds.iter().map(|&(n, h)| (n, (top - h) / grade)).collect::<Vec<_>>());
        let lifts: Vec<(usize, f64)> = seeds.iter().filter(|&&(n, _)| !ends[n].is_empty())
            .map(|&(n, h)| (n, h - np_mean(&ends[n]))).filter(|&(_, v)| v > 0.0).collect();
        let lift_d = if lifts.is_empty() {
            None
        } else {
            let ltop = py_max_of(&lifts);
            Some((ltop, dijkstra(&adj, &lifts.iter().map(|&(n, v)| (n, (ltop - v) / grade)).collect::<Vec<_>>())))
        };
        for p in 0..np_ {
            let (a, b, s) = (edges[p].0, edges[p].1, &edges[p].2);
            let last = s[s.len() - 1];
            let reach = |i: usize| np_max(top - grade * (dd[a] + s[i]), top - grade * ((dd[b] + last) - s[i]));
            if kinds[p] == BRIDGE {
                let req: Vec<f64> = (0..s.len()).map(|i| np_max(zb[p][i], reach(i))).collect();
                zb[p] = envelope(s, &req, grade);
            } else if let Some((ltop, dl)) = &lift_d {
                let z = &zb[p];
                let out: Vec<f64> = (0..s.len()).map(|i| {
                    let lift = np_max(ltop - grade * (dl[a] + s[i]), ltop - grade * ((dl[b] + last) - s[i]));
                    np_min(z[i] + np_clip(lift, 0.0, c.seed_lift), np_max(z[i], reach(i)))
                }).collect();
                zb[p] = out;
                alias[p] = false;
            }
        }
    }
    Ok((zb, alias))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heap_matches_sorted_pops() {
        let adj = vec![vec![(1, 1.0)], vec![(0, 1.0), (2, 2.0)], vec![(1, 2.0)]];
        let d = dijkstra(&adj, &[(2, 0.0)]);
        assert_eq!(d, vec![3.0, 2.0, 0.0]);
    }

    #[test]
    fn pairwise_small() {
        assert_eq!(pairwise(&[1.0, 2.0, 3.0]), 6.0);
        assert!(!np_mean(&[-0.0]).is_sign_negative());
    }
}
