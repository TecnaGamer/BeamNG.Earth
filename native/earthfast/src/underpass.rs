//! grading.underpass_offsets: the dips of ground roads under short overpasses, along the roads joined to them (after
//! wyo-backroads' PR #374, whose Python ran a full-graph Dijkstra per crossing: ~15 s of downtown Los Angeles' 36 road
//! tiles before it was limited to the edges a dip reaches).

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::f64::consts::PI;

/// roads._EndJoin: points within tol of an earlier one join it (the nearest; the lower index on a tie).
struct Join {
    tol: f64,
    grid: HashMap<(i64, i64), Vec<usize>>,
    pts: Vec<(f64, f64)>,
}

impl Join {
    fn node(&mut self, x: f64, y: f64) -> usize {
        let (gx, gy) = ((x / self.tol).floor() as i64, (y / self.tol).floor() as i64);
        let (mut best, mut bd) = (usize::MAX, self.tol * self.tol);
        for dx in -1..=1 {
            for dy in -1..=1 {
                if let Some(v) = self.grid.get(&(gx + dx, gy + dy)) {
                    for &i in v {
                        let (qx, qy) = self.pts[i];
                        let d = (qx - x).powi(2) + (qy - y).powi(2);
                        if d < bd || (d == bd && best != usize::MAX && i < best) {
                            best = i;
                            bd = d;
                        }
                    }
                }
            }
        }
        if best != usize::MAX {
            return best;
        }
        self.pts.push((x, y));
        self.grid.entry((gx, gy)).or_default().push(self.pts.len() - 1);
        self.pts.len() - 1
    }
}

#[derive(PartialEq)]
struct Item(f64, usize);
impl Eq for Item {}
impl Ord for Item {
    fn cmp(&self, o: &Self) -> Ordering {
        o.0.total_cmp(&self.0).then_with(|| o.1.cmp(&self.1))
    }
}
impl PartialOrd for Item {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

/// Shortest distances along the edges from the seeds, only those up to limit (the rest stay infinite).
fn dijkstra(adj: &[Vec<(usize, f64)>], seeds: &[(usize, f64)], limit: f64, d: &mut [f64], touched: &mut Vec<usize>) {
    let mut q = BinaryHeap::new();
    for &(n, d0) in seeds {
        if d0 <= limit && d0 < d[n] {
            if d[n].is_infinite() {
                touched.push(n);
            }
            d[n] = d0;
            q.push(Item(d0, n));
        }
    }
    while let Some(Item(dd, u)) = q.pop() {
        if dd > d[u] {
            continue;
        }
        for &(v, w) in &adj[u] {
            let nd = dd + w;
            if nd <= limit && nd < d[v] {
                if d[v].is_infinite() {
                    touched.push(v);
                }
                d[v] = nd;
                q.push(Item(nd, v));
            }
        }
    }
}

fn clip01(v: f64) -> f64 {
    v.clamp(0.0, 1.0)
}

enum Req {
    // a part joined to nothing: grading.under_dip's ramps, made shorter (up to max_grade) to end inside it
    Alone { sc: f64, depth: f64, r0: f64, r1: f64 },
    // a dip spreading along the joined roads: distances from the crossing to the nodes it reaches
    Joined { part: usize, sc: f64, depth: f64, run: f64, dist: HashMap<usize, f64> },
}

/// Offsets (m, <= 0) of points along ground road lines for the requirements (line, arc m, depth m) of the
/// overpasses they cross under.
/// - The lines (xy, cut by starts) join where their end points are within tol of each other.
/// - A requirement on a line joined to nothing: flat for flat m either side of the crossing, then a cosine ramp
///   back up whose steepest grade is grade, made steeper (up to max_grade) to end inside the line, or less deep
///   (to what fits; none under 0.3 m).
/// - Else the dip runs along the joined lines by distance through the junctions: flat, then a cosine ramp at half
///   grade, easing to 0 towards a loose end (a node of one line).
/// - Several dips: the 8-norm of their depths, capped at max_drop.
/// - At a junction of 3+ lines (no loose end within jflat + jfade, no crossing within jflat + jfade + flat): every
///   line there takes the deepest drop within jflat of it, blended back over jfade (smoothstep).
/// The queries (qxy, qline) are points on those lines: each is placed at the nearest point of its line.
/// prm: [flat, grade, max_grade, max_drop, jflat, jfade, tol]. Pure and deterministic for a given input order.
#[pyfunction]
pub fn underpass_offsets<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    starts: PyReadonlyArray1<'py, i64>,
    req: PyReadonlyArray2<'py, f64>,
    qxy: PyReadonlyArray2<'py, f64>,
    qline: PyReadonlyArray1<'py, i64>,
    prm: PyReadonlyArray1<'py, f64>,
) -> Bound<'py, PyArray1<f64>> {
    let (xy, starts, req, qxy, qline, prm) =
        (xy.as_array(), starts.as_array(), req.as_array(), qxy.as_array(), qline.as_array(), prm.as_array());
    let (flat, grade, max_grade, max_drop, jflat, jfade, tol) = (prm[0], prm[1], prm[2], prm[3], prm[4], prm[5], prm[6]);
    let nl = starts.len().saturating_sub(1);
    let nq = qline.len();
    let mut out = vec![0.0f64; nq];
    if nl == 0 || req.shape()[0] == 0 {
        return out.into_pyarray(py);
    }
    let rng = |l: usize| starts[l] as usize..starts[l + 1] as usize;
    let len: Vec<f64> = (0..nl)
        .map(|l| {
            let r = rng(l);
            (r.start + 1..r.end).map(|k| (xy[[k, 0]] - xy[[k - 1, 0]]).hypot(xy[[k, 1]] - xy[[k - 1, 1]])).sum()
        })
        .collect();
    // the graph: nodes at the joined end points, an edge per line
    let mut join = Join { tol, grid: HashMap::new(), pts: Vec::new() };
    let mut ends = Vec::with_capacity(nl);
    for l in 0..nl {
        let r = rng(l);
        if r.is_empty() {
            ends.push((usize::MAX, usize::MAX));
            continue;
        }
        let a = join.node(xy[[r.start, 0]], xy[[r.start, 1]]);
        let b = join.node(xy[[r.end - 1, 0]], xy[[r.end - 1, 1]]);
        ends.push((a, b));
    }
    let nn = join.pts.len();
    let mut adj: Vec<Vec<(usize, f64)>> = vec![Vec::new(); nn];
    let mut inc: Vec<Vec<usize>> = vec![Vec::new(); nn];
    let mut degree = vec![0usize; nn];
    for (l, &(a, b)) in ends.iter().enumerate() {
        if a == usize::MAX {
            continue;
        }
        adj[a].push((b, len[l]));
        adj[b].push((a, len[l]));
        inc[a].push(l);
        inc[b].push(l);
        degree[a] += 1;
        degree[b] += 1;
    }
    let max_run = max_drop * PI / grade;
    let mut buf = vec![f64::INFINITY; nn];
    let mut touched = Vec::new();
    // distance to the nearest loose end (only up to the longest ramp: past it a ramp never eases)
    let loose: Vec<(usize, f64)> = (0..nn).filter(|&n| degree[n] == 1).map(|n| (n, 0.0)).collect();
    let mut terminal = vec![f64::INFINITY; nn];
    dijkstra(&adj, &loose, flat + max_run + 1.0, &mut terminal, &mut touched);
    touched.clear();
    let jreach = jflat + jfade + flat;
    let mut source = vec![f64::INFINITY; nn];
    let mut reqs: Vec<Req> = Vec::new();
    let mut by_line: HashMap<usize, Vec<usize>> = HashMap::new();
    for r in 0..req.shape()[0] {
        let (part, sc, depth) = (req[[r, 0]] as usize, req[[r, 1]], req[[r, 2]]);
        if part >= nl || ends[part].0 == usize::MAX || !(depth > 0.0) {
            continue;
        }
        let (a, b) = ends[part];
        let ll = len[part];
        let run = depth * PI / grade;
        let limit = (flat + run + 1.0).max(jreach + 1.0);
        dijkstra(&adj, &[(a, sc), (b, ll - sc)], limit, &mut buf, &mut touched);
        let mut dist = HashMap::with_capacity(touched.len());
        for &n in &touched {
            source[n] = source[n].min(buf[n]);
            dist.insert(n, buf[n]);
            buf[n] = f64::INFINITY;
        }
        touched.clear();
        if degree[a] == 1 && degree[b] == 1 {
            let room = [sc, ll - sc];
            let runs = |depth: f64| -> Option<(f64, f64)> {
                let run = depth * PI / (2.0 * grade);
                let mut rr = [0.0; 2];
                for (k, r) in room.iter().enumerate() {
                    let avail = r - flat;
                    if avail >= run {
                        rr[k] = run;
                    } else if avail > 0.0 && depth * PI / (2.0 * avail) <= max_grade {
                        rr[k] = avail;
                    } else {
                        return None;
                    }
                }
                Some((rr[0], rr[1]))
            };
            let got = match runs(depth) {
                Some(rr) => Some((depth, rr)),
                None => {
                    let fit = (room[0].min(room[1]) - flat).max(0.0) * 2.0 * max_grade / PI;
                    if fit < 0.3 {
                        None
                    } else {
                        runs(depth.min(fit)).map(|rr| (depth.min(fit), rr))
                    }
                }
            };
            if let Some((d, (r0, r1))) = got {
                by_line.entry(part).or_default().push(reqs.len());
                reqs.push(Req::Alone { sc, depth: d, r0, r1 });
            }
            continue;
        }
        let mut lines: Vec<usize> = vec![part];
        for (&n, &d) in &dist {
            if d < flat + run + 1.0 {
                lines.extend(inc[n].iter().copied());
            }
        }
        lines.sort_unstable();
        lines.dedup();
        for l in lines {
            by_line.entry(l).or_default().push(reqs.len());
        }
        reqs.push(Req::Joined { part, sc, depth, run, dist });
    }
    let base = |l: usize, s: f64| -> f64 {
        let Some(rs) = by_line.get(&l) else { return 0.0 };
        let (ea, eb) = ends[l];
        let ll = len[l];
        let mut power = 0.0f64;
        for &r in rs {
            match &reqs[r] {
                Req::Alone { sc, depth, r0, r1 } => {
                    let d = s - sc;
                    let rr = if d < 0.0 { *r0 } else { *r1 };
                    let u = clip01((d.abs() - flat) / rr.max(1e-9));
                    power += (depth * 0.5 * (1.0 + (PI * u).cos())).powi(8);
                }
                Req::Joined { part, sc, depth, run, dist } => {
                    let da = dist.get(&ea).copied().unwrap_or(f64::INFINITY);
                    let db = dist.get(&eb).copied().unwrap_or(f64::INFINITY);
                    let mut d = (da + s).min(db + ll - s);
                    if l == *part {
                        d = d.min((s - sc).abs());
                    }
                    let u = clip01((d - flat) / run.max(1e-9));
                    let mut drop = depth * 0.5 * (1.0 + (PI * u).cos());
                    let room = (terminal[ea] + s).min(terminal[eb] + ll - s);
                    let v = clip01(room / run.max(1e-9));
                    drop = drop.min(depth * 0.5 * (1.0 - (PI * v).cos()));
                    power += drop.powi(8);
                }
            }
        }
        power.powf(1.0 / 8.0).min(max_drop)
    };
    // the junctions' common drops (made when a query needs one)
    let mut common: HashMap<usize, f64> = HashMap::new();
    let mut common_of = |n: usize| -> f64 {
        if let Some(&c) = common.get(&n) {
            return c;
        }
        let mut c = 0.0f64;
        for &l in &inc[n] {
            let ll = len[l];
            for (end, at) in [(ends[l].0, true), (ends[l].1, false)] {
                if end != n {
                    continue;
                }
                for t in 0..=(jflat.floor() as i64) {
                    let t = (t as f64).min(ll);
                    c = c.max(base(l, if at { t } else { ll - t }));
                }
            }
        }
        common.insert(n, c);
        c
    };
    let joint = |n: usize| degree[n] > 2 && terminal[n] > jflat + jfade && source[n] > jreach;
    // the queries, line by line
    let mut order: Vec<usize> = (0..nq).collect();
    order.sort_by_key(|&q| qline[q]);
    let mut k = 0;
    while k < nq {
        let l = qline[order[k]];
        let mut e = k;
        while e < nq && qline[order[e]] == l {
            e += 1;
        }
        if l < 0 || l as usize >= nl || ends[l as usize].0 == usize::MAX {
            k = e;
            continue;
        }
        let l = l as usize;
        let (ea, eb) = ends[l];
        let mut nodes: Vec<(usize, bool)> = vec![(ea, true), (eb, false)];
        nodes.sort_by_key(|&(n, at)| (n, !at));
        let joints: Vec<(bool, f64)> =
            nodes.iter().filter(|&&(n, _)| joint(n)).map(|&(n, at)| (at, common_of(n))).collect();
        if !by_line.contains_key(&l) && joints.iter().all(|&(_, c)| c <= 0.0) {
            k = e;
            continue;
        }
        let r = rng(l);
        let ll = len[l];
        let mut arc = Vec::with_capacity(r.len());
        arc.push(0.0);
        for k2 in r.start + 1..r.end {
            arc.push(arc.last().unwrap() + (xy[[k2, 0]] - xy[[k2 - 1, 0]]).hypot(xy[[k2, 1]] - xy[[k2 - 1, 1]]));
        }
        for &q in &order[k..e] {
            let (px, py_) = (qxy[[q, 0]], qxy[[q, 1]]);
            // the nearest point of the line
            let (mut bd, mut s) = (f64::INFINITY, 0.0);
            if r.len() == 1 {
                s = 0.0;
            }
            for j in r.start..r.end.saturating_sub(1) {
                let (ax, ay, bx, by) = (xy[[j, 0]], xy[[j, 1]], xy[[j + 1, 0]], xy[[j + 1, 1]]);
                let (abx, aby) = (bx - ax, by - ay);
                let t = clip01(((px - ax) * abx + (py_ - ay) * aby) / (abx * abx + aby * aby).max(1e-12));
                let d = (ax + t * abx - px).powi(2) + (ay + t * aby - py_).powi(2);
                if d < bd {
                    bd = d;
                    let i = j - r.start;
                    s = arc[i] + t * (arc[i + 1] - arc[i]);
                }
            }
            let mut drop = base(l, s);
            for &(at, c) in &joints {
                let u = clip01(((if at { s } else { ll - s }) - jflat) / jfade);
                let blend = u * u * (3.0 - 2.0 * u);
                drop = c * (1.0 - blend) + drop * blend;
            }
            out[q] = -drop;
        }
        k = e;
    }
    out.into_pyarray(py)
}
