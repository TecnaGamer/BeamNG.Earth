//! grading.grade_guard: the last pass over a cell's road plans, on the finished profiles of all of them at once.
//!
//! Every earlier pass (profile smoothing, junction snaps, approach lifts, underpass dips, a solved interchange) works
//! on one road or one junction and trusts its own limits; where their results meet, a road can be left standing ten
//! metres over the side road that joins it, or a DEM spike can sit on a motorway. This pass looks at the roads as one
//! graph and keeps what a real road cannot do out of the plans:
//!
//! - **one height a node**: the ends of plans that meet at a point are brought to one height (a bridge end's; else the
//!   road they run into; else the top rank's, by length), each road easing to it along its own chain over a cosine as
//!   long as its grade ceiling and a vertical curve need, so that it changes height smoothly (see `equalise_nodes`);
//! - a **window rule**: where a point's neighbours `win_n` m before and after stand within `win_h` of each other, the
//!   point may not stand more than `win_h` outside them (no bump or pit a road of that class could not have);
//! - a **grade ceiling** per class (`gmax`): the profile of every road is brought under it, and so is the step to the
//!   road it joins (the end of a side road is tied to the nearest vertex of the road it meets, within a cross slope);
//! - what the ground itself has is left alone: a stretch may be as steep as the natural profile (`base`, the plan
//!   before the later passes) was there plus 2 %, so a real pass road is not flattened, only what the passes added;
//! - designed shapes (a point more than `designed_tol` from its natural height: an approach fill, an underpass dip) and
//!   bridge abutments are stiff: the roads around them give way first, so a clearance they were solved for stays.
//!
//! The changes are made by weighted projection onto the grade limits (the stiffer a point, the less it moves; weight =
//! road rank), the change eased by a box blur, and a final projection to the full ceiling. Where the limits cannot be
//! met (a ramp that has no room: the other end of the road is pinned too), the least violated profile is returned and
//! the worst remaining violation is in the stats, for the caller to log. Pure and deterministic.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// What a designed point's weight is multiplied by (it moves 1/DESIGNED_W as far as a natural one).
const DESIGNED_W: f64 = 50.0;
/// A designed shape's own grade is kept up to this (the solved ramps and dips are at 10 % at most; a step is no ramp).
const DESIGNED_MAX_GRADE: f64 = 0.12;
/// An abutment end of a plan: a bridge's profile was sampled there.
const ABUT_W: f64 = 1000.0;
/// A plan end that joins nothing (a dead end, or where the cell's clip cuts the road): it keeps its height.
const FREE_END_W: f64 = 20.0;
/// A plan the caller left out (gmax <= 0): its heights are fixed.
const FIXED_W: f64 = 1.0e6;
/// A natural slope may be exceeded by this much before the profile counts as steeper than the ground.
const NATURAL_SLOPE_ALLOW: f64 = 0.02;
/// The first projection aims this much under the ceiling, so that the blur does not take it over.
const AIM: f64 = 0.85;
/// A junction link may differ by this much (parts of one road meet at one height, as the junction snaps and the chain
/// extensions leave them) plus a cross slope of LINK_CROSS over the distance to the joined road's line.
const LINK_BASE_M: f64 = 0.005;
const LINK_CROSS: f64 = 0.04;
/// Ends of plans this close stand at one node.
const NODE_R_M: f64 = 1.5;
/// A node's ends this far apart in height are a step to take out.
const NODE_MIN_STEP_M: f64 = 0.005;
/// Bridge ends of one node this far apart disagree: the node is left, the stats' violations show it.
const NODE_PIN_TOL_M: f64 = 0.3;
/// The steepest curvature of an eased road (1/m: a vertical curve of 500 m radius, the grade bends 4 % over 20 m).
const EASE_CURVATURE: f64 = 0.002;
/// An ease is at least this long (a small step over a short ease is a crease all the same).
const EASE_MIN_M: f64 = 30.0;
/// What an ease may add to the road's own grade, whatever its ceiling leaves.
const EASE_MIN_ADD: f64 = 0.02;
/// m along the road its own grade is taken over.
const EASE_OWN_M: f64 = 60.0;
const BLUR_R: usize = 4;
const BLUR_PASSES: usize = 3;

pub struct Inp<'a> {
    pub xy: &'a [[f64; 2]],
    pub z: &'a [f64],
    pub base: &'a [f64],
    pub off: &'a [usize],
    pub gmax: &'a [f64],
    pub win_n: &'a [f64],
    pub win_h: &'a [f64],
    pub rank: &'a [i64],
    pub half: &'a [f64],
    pub abut: &'a [[bool; 2]],
}

#[derive(Default, Debug)]
pub struct Stats {
    pub changed_pts: usize,
    pub max_change: f64,
    pub viol_before: usize,
    pub max_viol_before: f64,
    pub viol_after: usize,
    pub max_viol_after: f64,
    pub worst_after: usize,
    pub nodes_levelled: usize,
}

struct Edge {
    i: usize,
    j: usize,
    aim: f64, // allowed |dz| in the first projection
    tol: f64, // allowed |dz| in the end
}

/// Moves z[i] and z[j] together until |dz| <= tol, each by its share (the stiffer one moves less). Returns the excess.
#[inline]
fn project(z: &mut [f64], w: &[f64], i: usize, j: usize, tol: f64) -> f64 {
    let d = z[j] - z[i];
    let ex = d.abs() - tol;
    if ex <= 0.0 {
        return 0.0;
    }
    let (wi, wj) = (w[i], w[j]);
    let si = ex * wj / (wi + wj);
    let sj = ex - si;
    let sg = d.signum();
    z[i] += sg * si;
    z[j] -= sg * sj;
    ex
}

/// Bounds from the points already fixed: u[i] <= min over fixed j of z_j + (the edges' limits along the shortest way),
/// l[i] >= max of z_j - the same. Relaxed along the chains and the links until nothing changes (a few passes: the
/// graph is chains with a few links).
fn relax(u: &mut [f64], l: &mut [f64], chain: &[Edge], links: &[Edge], fin: bool) {
    for _ in 0..12 {
        let mut ch = false;
        let mut step = |e: &Edge, rev: bool| {
            let t = if fin { e.tol } else { e.aim };
            let (i, j) = if rev { (e.j, e.i) } else { (e.i, e.j) };
            if u[i] + t < u[j] {
                u[j] = u[i] + t;
                ch = true;
            }
            if l[i] - t > l[j] {
                l[j] = l[i] - t;
                ch = true;
            }
        };
        for e in chain {
            step(e, false);
        }
        for e in chain.iter().rev() {
            step(e, true);
        }
        for e in links {
            step(e, false);
            step(e, true);
        }
        if !ch {
            break;
        }
    }
}

/// The points one weight level at a time, the stiffest first: each level's points are put inside the bounds the stiffer
/// ones (fixed by then) leave them, brought under the limits between each other, and then fixed themselves. A road
/// that gives way to a stiffer one gives way exactly as far as the limits need (a ramp as long as its grade ceiling
/// makes it), not as far as a slow iteration reached. Returns nothing: z is changed in place.
fn solve(z: &mut [f64], w: &[f64], chain: &[Edge], links: &[Edge], fin: bool) {
    let n = z.len();
    let mut u = vec![f64::INFINITY; n];
    let mut l = vec![f64::NEG_INFINITY; n];
    let mut levels: Vec<f64> = w.to_vec();
    levels.sort_by(|a, b| b.partial_cmp(a).unwrap());
    levels.dedup();
    for &lv in &levels {
        let pts: Vec<usize> = (0..n).filter(|&i| w[i] == lv).collect();
        for &i in &pts {
            if l[i] > u[i] {
                z[i] = 0.5 * (l[i] + u[i]); // the stiffer ones disagree here: halfway (the stats show it)
            } else {
                z[i] = z[i].clamp(l[i], u[i]);
            }
        }
        // between the level's own points (an edge to a stiffer point is in the bounds already)
        let own: Vec<&Edge> = chain.iter().chain(links.iter()).filter(|e| w[e.i] == lv && w[e.j] == lv).collect();
        if !own.is_empty() {
            for _ in 0..30 {
                let mut worst: f64 = 0.0;
                for e in own.iter() {
                    let t = if fin { e.tol } else { e.aim };
                    worst = worst.max(project(z, w, e.i, e.j, t));
                }
                for e in own.iter().rev() {
                    let t = if fin { e.tol } else { e.aim };
                    worst = worst.max(project(z, w, e.i, e.j, t));
                }
                if worst < 1e-3 {
                    break;
                }
            }
            for &i in &pts {
                z[i] = z[i].clamp(l[i].min(u[i]), u[i].max(l[i]));
            }
        }
        for &i in &pts {
            u[i] = z[i];
            l[i] = z[i];
        }
        relax(&mut u, &mut l, chain, links, fin);
    }
}

fn box_blur(d: &mut [f64], r: usize, passes: usize) {
    let n = d.len();
    if n < 3 {
        return;
    }
    let mut tmp = vec![0.0; n];
    for _ in 0..passes {
        for i in 0..n {
            let a = i.saturating_sub(r);
            let b = (i + r).min(n - 1);
            // a window cut short at the ends is kept symmetric, so the end values stay put
            let k = (i - a).min(b - i);
            let (a, b) = (i - k, i + k);
            let mut s = 0.0;
            for v in &d[a..=b] {
                s += v;
            }
            tmp[i] = s / (b - a + 1) as f64;
        }
        d.copy_from_slice(&tmp);
    }
}

/// The worst excess over each edge's final limit, how many edges exceed it, and the point of the worst.
fn violations(z: &[f64], chain: &[Edge], links: &[Edge]) -> (usize, f64, usize) {
    let (mut n, mut worst, mut at) = (0usize, 0.0f64, 0usize);
    for e in chain.iter().chain(links.iter()) {
        let ex = (z[e.j] - z[e.i]).abs() - e.tol;
        if ex > 0.02 {
            n += 1;
            if ex > worst {
                worst = ex;
                at = e.i;
            }
        }
    }
    (n, worst, at)
}

struct Ends {
    node: Vec<usize>, // per end (2 * plan + which): its node, or usize::MAX
    members: Vec<Vec<usize>>,
}

fn root(p: &mut [usize], mut a: usize) -> usize {
    while p[a] != a {
        p[a] = p[p[a]];
        a = p[a];
    }
    a
}

/// The ends of the plans (2 * plan + 0 / 1) grouped into nodes of two or more, by distance (a grid and a union-find).
fn find_nodes(inp: &Inp) -> Ends {
    let np = inp.off.len() - 1;
    let pt = |e: usize| if e % 2 == 0 { inp.off[e / 2] } else { inp.off[e / 2 + 1] - 1 };
    let good = |e: usize| inp.off[e / 2 + 1] - inp.off[e / 2] >= 2;
    let mut grid: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
    let mut parent: Vec<usize> = (0..2 * np).collect();
    for e in (0..2 * np).filter(|&e| good(e)) {
        let [x, y] = inp.xy[pt(e)];
        let (cx, cy) = ((x / NODE_R_M).floor() as i64, (y / NODE_R_M).floor() as i64);
        for dx in -1..=1 {
            for dy in -1..=1 {
                if let Some(v) = grid.get(&(cx + dx, cy + dy)) {
                    for &o in v {
                        let [ox, oy] = inp.xy[pt(o)];
                        if (ox - x).hypot(oy - y) <= NODE_R_M {
                            let (a, b) = (root(&mut parent, o), root(&mut parent, e));
                            parent[b] = a;
                        }
                    }
                }
            }
        }
        grid.entry((cx, cy)).or_default().push(e);
    }
    let mut groups: std::collections::BTreeMap<usize, Vec<usize>> = std::collections::BTreeMap::new();
    for e in (0..2 * np).filter(|&e| good(e)) {
        let r = root(&mut parent, e);
        groups.entry(r).or_default().push(e);
    }
    let mut node = vec![usize::MAX; 2 * np];
    let mut members = Vec::new();
    for v in groups.into_values() {
        if v.len() >= 2 {
            for &e in &v {
                node[e] = members.len();
            }
            members.push(v);
        }
    }
    Ends { node, members }
}

/// A cosine from `step` at plan end `e` to nothing, added to z along the road: through the plan and, where it ends in a
/// plain continuation (a node of two ends), on into the next part. It stops at a bridge end, a road that stays out, a
/// free end or a bigger node, where the road must keep its height: the ease is then as long as there is room.
fn ease_end(inp: &Inp, nodes: &Ends, s: &[f64], z: &mut [f64], e: usize, step: f64) {
    let start = e / 2;
    let (o0, o1) = (inp.off[start], inp.off[start + 1]);
    let own_at = |h: &[f64]| {
        let (a, b) = if e % 2 == 0 {
            (o0, (o0..o1).find(|&i| s[i] - s[o0] >= EASE_OWN_M).unwrap_or(o1 - 1))
        } else {
            ((o0..o1).rev().find(|&i| s[o1 - 1] - s[i] >= EASE_OWN_M).unwrap_or(o0), o1 - 1)
        };
        (h[b] - h[a]).abs() / (s[b] - s[a]).max(1.0)
    };
    let (own, natural) = (own_at(&*z), own_at(inp.base));
    let allowed = (AIM * inp.gmax[start]).max(natural + NATURAL_SLOPE_ALLOW);
    let add = (allowed - own).max(EASE_MIN_ADD);
    let pi = std::f64::consts::PI;
    let end_i = if e % 2 == 0 { o0 } else { o1 - 1 };
    let (now, target) = (z[end_i] - inp.base[end_i], z[end_i] + step - inp.base[end_i]); // over the natural height
    let d = step.abs().max(target.abs().min(now.abs() + step.abs()));
    let want = (pi * d / (2.0 * add)).max(pi * (d / (2.0 * EASE_CURVATURE)).sqrt()).max(EASE_MIN_M);
    // the way along the chain: (plan, entered at its first point?)
    let mut way: Vec<(usize, bool)> = vec![(start, e % 2 == 0)];
    let mut room = 0.0;
    loop {
        let (p, fwd) = *way.last().unwrap();
        room += s[inp.off[p + 1] - 1];
        if room >= want {
            break;
        }
        let far = 2 * p + fwd as usize;
        let n = nodes.node[far];
        if n == usize::MAX || nodes.members[n].len() != 2 || inp.abut[p][far % 2] {
            break;
        }
        let next = if nodes.members[n][0] == far { nodes.members[n][1] } else { nodes.members[n][0] };
        let q = next / 2;
        if inp.gmax[q] <= 0.0 || inp.abut[q][next % 2] || way.iter().any(|w| w.0 == q) {
            break;
        }
        way.push((q, next % 2 == 0));
    }
    let len = want.min(room).max(1e-6);
    let mut span: Vec<(usize, f64)> = Vec::new();
    let mut walked = 0.0;
    for &(p, fwd) in &way {
        let (a, b) = (inp.off[p], inp.off[p + 1]);
        let total = s[b - 1];
        for i in a..b {
            let along = walked + if fwd { s[i] } else { total - s[i] };
            if along < len {
                span.push((i, along));
            }
        }
        walked += total;
    }
    // an earlier pass's taper at this end (nothing but a fall from the end's change to the far side's): the ease takes
    // its place, or the taper's short crease would stay inside the ease. Any other shape there is kept, the ease added.
    let far_off = span.iter().max_by(|a, b| a.1.total_cmp(&b.1)).map_or(0.0, |&(i, _)| z[i] - inp.base[i]);
    let reach_off = (now - far_off).abs().max((target - far_off).abs()) * 1.2 + 0.05;
    let taper = span.iter().all(|&(i, _)| (z[i] - inp.base[i] - far_off).abs() <= reach_off);
    for &(i, along) in &span {
        let c = 0.5 * (1.0 + (pi * along / len).cos());
        if taper {
            z[i] = inp.base[i] + far_off + (target - far_off) * c;
        } else {
            z[i] += step * c;
        }
    }
}

/// One height at every node. The height: a bridge end's or a road left out's if the node has one; else the height of the
/// road of at least its rank that its ends run into; else the top rank's ends, weighted by length. A node whose natural
/// heights are already over `node_max` apart is the data's own step (Perm, GitHub #414) and stays. Higher ranks go first,
/// so that a lower road's node meets the height they settled on. Returns the nodes levelled.
fn equalise_nodes(inp: &Inp, nodes: &Ends, links: &[Edge], s: &[f64], z: &mut [f64], node_max: f64) -> usize {
    let plan_of = |pt: usize| inp.off.partition_point(|&o| o <= pt) - 1;
    let end_pt = |e: usize| if e % 2 == 0 { inp.off[e / 2] } else { inp.off[e / 2 + 1] - 1 };
    let top = |n: usize| nodes.members[n].iter().map(|&e| inp.rank[e / 2]).max().unwrap_or(0);
    let mut order: Vec<usize> = (0..nodes.members.len()).collect();
    order.sort_by_key(|&n| -top(n));
    let mut done = 0;
    for n in order {
        let m = &nodes.members[n];
        let (mut lo, mut hi) = (f64::INFINITY, f64::NEG_INFINITY);
        let (mut blo, mut bhi) = (f64::INFINITY, f64::NEG_INFINITY);
        for &e in m {
            let i = end_pt(e);
            lo = lo.min(z[i]);
            hi = hi.max(z[i]);
            blo = blo.min(inp.base[i]);
            bhi = bhi.max(inp.base[i]);
        }
        if hi - lo <= NODE_MIN_STEP_M || bhi - blo > node_max {
            continue;
        }
        let pinned = |e: usize| inp.abut[e / 2][e % 2] || inp.gmax[e / 2] <= 0.0;
        let pins: Vec<f64> = m.iter().filter(|&&e| pinned(e)).map(|&e| z[end_pt(e)]).collect();
        let tn = top(n);
        let target = if !pins.is_empty() {
            let (a, b) = pins.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |t, v| (t.0.min(*v), t.1.max(*v)));
            if b - a > NODE_PIN_TOL_M {
                continue;
            }
            pins.iter().sum::<f64>() / pins.len() as f64
        } else if let Some(v) = links.iter().find_map(|l| {
            let k = plan_of(l.j);
            let inner = l.j != inp.off[k] && l.j != inp.off[k + 1] - 1;
            (inner && m.iter().any(|&e| end_pt(e) == l.i) && inp.rank[k] >= tn).then(|| z[l.j])
        }) {
            v
        } else {
            let (mut sw, mut sz) = (0.0, 0.0);
            for &e in m.iter().filter(|&&e| inp.rank[e / 2] == tn) {
                let l = s[inp.off[e / 2 + 1] - 1].max(1.0);
                sw += l;
                sz += l * z[end_pt(e)];
            }
            sz / sw
        };
        for &e in m {
            let step = target - z[end_pt(e)];
            if !pinned(e) && step.abs() > 1e-4 {
                ease_end(inp, nodes, s, z, e, step);
            }
        }
        done += 1;
    }
    done
}

pub fn guard(inp: &Inp, reach: f64, designed_tol: f64, node_max: f64) -> (Vec<f64>, Stats) {
    let n = inp.z.len();
    let np = inp.off.len() - 1;
    let mut z: Vec<f64> = inp.z.to_vec();
    let z_in = z.clone();
    let mut w = vec![1.0; n];
    let mut s = vec![0.0; n];
    for p in 0..np {
        let (o0, o1) = (inp.off[p], inp.off[p + 1]);
        let wr = 1.0 + inp.rank[p].max(0) as f64;
        for i in o0..o1 {
            if i > o0 {
                s[i] = s[i - 1] + (inp.xy[i][0] - inp.xy[i - 1][0]).hypot(inp.xy[i][1] - inp.xy[i - 1][1]);
            }
            w[i] = if inp.gmax[p] <= 0.0 {
                FIXED_W
            } else if (z[i] - inp.base[i]).abs() > designed_tol {
                wr * DESIGNED_W
            } else {
                wr
            };
        }
        if o1 > o0 {
            if inp.abut[p][0] {
                w[o0] = w[o0].max(ABUT_W);
            }
            if inp.abut[p][1] {
                w[o1 - 1] = w[o1 - 1].max(ABUT_W);
            }
        }
    }

    // links: the end of a plan to the nearest vertex of each other plan whose line it touches
    let mut bb = vec![[f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY]; np];
    for p in 0..np {
        for i in inp.off[p]..inp.off[p + 1] {
            let b = &mut bb[p];
            b[0] = b[0].min(inp.xy[i][0]);
            b[1] = b[1].min(inp.xy[i][1]);
            b[2] = b[2].max(inp.xy[i][0]);
            b[3] = b[3].max(inp.xy[i][1]);
        }
    }
    let mut links: Vec<Edge> = Vec::new();
    let mut linked = vec![false; n];
    for j in 0..np {
        let (o0, o1) = (inp.off[j], inp.off[j + 1]);
        if o1 - o0 < 2 || inp.gmax[j] <= 0.0 {
            continue;
        }
        for &e in &[o0, o1 - 1] {
            let (x, y) = (inp.xy[e][0], inp.xy[e][1]);
            for k in 0..np {
                if k == j || inp.off[k + 1] - inp.off[k] < 2 || inp.gmax[k] <= 0.0 {
                    continue;
                }
                let lim = inp.half[k] + reach;
                let b = &bb[k];
                if x < b[0] - lim || x > b[2] + lim || y < b[1] - lim || y > b[3] + lim {
                    continue;
                }
                let (mut bv, mut bd) = (usize::MAX, f64::INFINITY);
                for v in inp.off[k]..inp.off[k + 1] {
                    let d = (inp.xy[v][0] - x).hypot(inp.xy[v][1] - y);
                    if d < bd {
                        bd = d;
                        bv = v;
                    }
                }
                if bd <= lim && (inp.base[e] - inp.base[bv]).abs() <= node_max {
                    let tol = LINK_BASE_M + LINK_CROSS * bd;
                    links.push(Edge { i: e, j: bv, aim: tol, tol });
                    linked[e] = true;
                }
            }
        }
    }
    for p in 0..np {
        let (o0, o1) = (inp.off[p], inp.off[p + 1]);
        if o1 - o0 < 2 || inp.gmax[p] <= 0.0 {
            continue;
        }
        for &e in &[o0, o1 - 1] {
            if !linked[e] {
                w[e] = w[e].max(FREE_END_W);
            }
        }
    }

    // one height at every node, before anything else looks at the profiles
    let nodes = find_nodes(inp);
    let levelled = equalise_nodes(inp, &nodes, &links, &s, &mut z, node_max);
    let z_nodes = z.clone();

    // chain edges (along each plan), with the ground's own slope allowed
    let mut chain: Vec<Edge> = Vec::new();
    for p in 0..np {
        if inp.gmax[p] <= 0.0 {
            continue;
        }
        for i in inp.off[p]..inp.off[p + 1].saturating_sub(1) {
            let l = (s[i + 1] - s[i]).max(0.25);
            let bs = (inp.base[i + 1] - inp.base[i]).abs() / l;
            let tol = inp.gmax[p].max(bs + NATURAL_SLOPE_ALLOW) * l;
            let aim = (AIM * inp.gmax[p]).max(bs + NATURAL_SLOPE_ALLOW) * l;
            let (mut tol, mut aim) = (tol, aim.min(tol));
            if w[i] >= DESIGNED_W || w[i + 1] >= DESIGNED_W {
                // a solved shape keeps the grade it was solved at (the passes that made it kept to their own limits)
                let own = ((z_nodes[i + 1] - z_nodes[i]).abs() + 0.01).min(DESIGNED_MAX_GRADE * l);
                tol = tol.max(own);
                aim = aim.max(own);
            }
            chain.push(Edge { i, j: i + 1, aim, tol });
        }
    }

    let (vb, mvb, _) = violations(&z_in, &chain, &links);

    // 1. the window rule, on the profile as it came
    let zs = z.clone();
    for p in 0..np {
        if inp.gmax[p] <= 0.0 || inp.win_n[p] <= 0.0 {
            continue;
        }
        let (o0, o1) = (inp.off[p], inp.off[p + 1]);
        let (nw, h) = (inp.win_n[p], inp.win_h[p]);
        let (mut ia, mut ib) = (o0, o0);
        for i in o0..o1 {
            while ia + 1 < i && s[ia + 1] <= s[i] - nw {
                ia += 1;
            }
            if ib < i {
                ib = i;
            }
            while ib < o1 && s[ib] < s[i] + nw {
                ib += 1;
            }
            if ib >= o1 || s[i] - s[o0] < nw || s[ia] > s[i] - nw {
                continue;
            }
            if (z_in[i] - inp.base[i]).abs() > designed_tol {
                continue;
            }
            let (a, b) = (zs[ia], zs[ib]);
            if (a - b).abs() <= h {
                z[i] = zs[i].clamp(a.min(b) - h, a.max(b) + h);
            }
        }
    }

    // 2. the grade ceiling, aiming a little under it
    let z_win = z.clone();
    solve(&mut z, &w, &chain, &links, false);

    // 3. what the ceiling changed, eased (the stiff points keep their own change)
    let mut delta: Vec<f64> = (0..n).map(|i| z[i] - z_win[i]).collect();
    for p in 0..np {
        let (o0, o1) = (inp.off[p], inp.off[p + 1]);
        if inp.gmax[p] <= 0.0 || o1 - o0 < 3 {
            continue;
        }
        let mut d: Vec<f64> = delta[o0..o1].to_vec();
        if d.iter().all(|v| v.abs() < 1e-6) {
            continue;
        }
        box_blur(&mut d, BLUR_R, BLUR_PASSES);
        for (k, v) in d.iter().enumerate() {
            if w[o0 + k] < DESIGNED_W {
                delta[o0 + k] = *v;
            }
        }
    }
    for i in 0..n {
        z[i] = z_win[i] + delta[i];
    }

    // 4. the full ceiling
    solve(&mut z, &w, &chain, &links, true);

    let (va, mva, at) = violations(&z, &chain, &links);
    let mut st = Stats {
        viol_before: vb,
        max_viol_before: mvb,
        viol_after: va,
        max_viol_after: mva,
        worst_after: at,
        nodes_levelled: levelled,
        ..Default::default()
    };
    for i in 0..n {
        let d = (z[i] - z_in[i]).abs();
        if d > 0.01 {
            st.changed_pts += 1;
            st.max_change = st.max_change.max(d);
        }
    }
    (z, st)
}

/// grading.grade_guard: (z (n,), stats (8,), changed (plans,)). xy (n, 2) the plans' points one after the other, off
/// (plans + 1,) their offsets; z the heights after every pass, base the natural profiles (before the passes); per plan:
/// gmax (the grade ceiling, <= 0 leaves the plan as it is), win_n / win_h (the window rule, win_n <= 0: none), rank (the
/// junction rank), half (the carriageway's half width), abut (a bridge at its first / last point). reach: m past a road's
/// edge an end still joins it; designed_tol: m from the natural height from which a point counts as designed;
/// node_max: m of natural height difference over which a node is the data's own step and stays.
/// stats: changed points, largest change, edges over the ceiling before / their worst excess, after / worst excess,
/// the global index of the worst point after, nodes levelled.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn grade_guard<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    base: PyReadonlyArray1<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    gmax: PyReadonlyArray1<'py, f64>,
    win_n: PyReadonlyArray1<'py, f64>,
    win_h: PyReadonlyArray1<'py, f64>,
    rank: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
    abut: PyReadonlyArray2<'py, u8>,
    reach: f64,
    designed_tol: f64,
    node_max: f64,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<u8>>)> {
    let xa = xy.as_array();
    let n = xa.nrows();
    let pts: Vec<[f64; 2]> = (0..n).map(|i| [xa[[i, 0]], xa[[i, 1]]]).collect();
    let zv = z.as_array().to_vec();
    let bv = base.as_array().to_vec();
    let off: Vec<usize> = off.as_array().iter().map(|&v| v.max(0) as usize).collect();
    let (g, wn, wh) = (gmax.as_array().to_vec(), win_n.as_array().to_vec(), win_h.as_array().to_vec());
    let (rk, hf) = (rank.as_array().to_vec(), half.as_array().to_vec());
    let ab = abut.as_array();
    let np = g.len();
    if off.len() != np + 1 || zv.len() != n || bv.len() != n || wn.len() != np || wh.len() != np || rk.len() != np
        || hf.len() != np || ab.nrows() != np || off[np] > n
    {
        return Err(pyo3::exceptions::PyValueError::new_err("grade_guard: array sizes differ"));
    }
    let abut_v: Vec<[bool; 2]> = (0..np).map(|k| [ab[[k, 0]] != 0, ab[[k, 1]] != 0]).collect();
    let inp = Inp { xy: &pts, z: &zv, base: &bv, off: &off, gmax: &g, win_n: &wn, win_h: &wh, rank: &rk, half: &hf,
                    abut: &abut_v };
    let (out, st) = guard(&inp, reach, designed_tol, node_max);
    let changed: Vec<u8> = (0..np)
        .map(|p| (off[p]..off[p + 1]).any(|i| (out[i] - zv[i]).abs() > 1e-6) as u8)
        .collect();
    let stats = vec![st.changed_pts as f64, st.max_change, st.viol_before as f64, st.max_viol_before,
                     st.viol_after as f64, st.max_viol_after, st.worst_after as f64, st.nodes_levelled as f64];
    Ok((out.into_pyarray(py), stats.into_pyarray(py), changed.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(x0: f64, y0: f64, dx: f64, dy: f64, n: usize) -> Vec<[f64; 2]> {
        (0..n).map(|i| [x0 + dx * i as f64, y0 + dy * i as f64]).collect()
    }

    fn run(plans: &[(Vec<[f64; 2]>, Vec<f64>, Vec<f64>, f64, f64, f64, i64, [bool; 2])], reach: f64) -> (Vec<f64>, Stats) {
        let (mut xy, mut z, mut base, mut off) = (vec![], vec![], vec![], vec![0usize]);
        let (mut g, mut wn, mut wh, mut rk, mut hf, mut ab) = (vec![], vec![], vec![], vec![], vec![], vec![]);
        for (p, zz, bb, gm, n, h, r, a) in plans {
            xy.extend(p.iter().cloned());
            z.extend(zz.iter().cloned());
            base.extend(bb.iter().cloned());
            off.push(xy.len());
            g.push(*gm);
            wn.push(*n);
            wh.push(*h);
            rk.push(*r);
            hf.push(5.0);
            ab.push(*a);
        }
        let inp = Inp { xy: &xy, z: &z, base: &base, off: &off, gmax: &g, win_n: &wn, win_h: &wh, rank: &rk,
                        half: &hf, abut: &ab };
        guard(&inp, reach, 0.3, 6.0)
    }

    fn max_slope(z: &[f64], step: f64) -> f64 {
        z.windows(2).map(|w| (w[1] - w[0]).abs() / step).fold(0.0, f64::max)
    }

    /// The Zhmerynka case: the main road raised 10 m at its node by an earlier pass, the side road that joins it
    /// left on the ground (a wall of 10 m): it must climb at its class's ceiling, and meet the main road.
    #[test]
    fn side_road_climbs_to_a_raised_main_road() {
        let main = line(0.0, -50.0, 0.0, 2.0, 51); // x = 0, y -50..50
        let mz: Vec<f64> = vec![10.0; 51];
        let mb: Vec<f64> = vec![0.0; 51];
        let side = line(0.0, 0.0, -2.0, 0.0, 151); // from the main road westward 300 m, at the ground
        let sz = vec![0.0; 151];
        let (z, st) = run(
            &[(main, mz, mb, 0.09, 0.0, 0.0, 14, [false, false]),
              (side, sz.clone(), sz, 0.12, 0.0, 0.0, 6, [false, false])],
            3.0,
        );
        let sz_out = &z[51..];
        assert!(sz_out[0] > 9.0, "the side road's end meets the main road: {}", sz_out[0]);
        assert!(max_slope(sz_out, 2.0) <= 0.121, "grade {}", max_slope(sz_out, 2.0));
        assert!(st.max_viol_after < 0.05, "{:?}", st);
    }

    /// A 3 m, 6 m wide spike of DEM noise on a motorway is taken out; the road around it stays.
    #[test]
    fn spike_on_a_motorway_is_removed() {
        let p = line(0.0, 0.0, 2.0, 0.0, 120);
        let mut z = vec![50.0; 120];
        for i in 58..61 {
            z[i] = 53.0;
        }
        let b = vec![50.0; 120];
        // (the spike is 3 m over base, so it counts as designed unless it is within the tolerance: base carries it)
        let b2: Vec<f64> = z.clone();
        let _ = b;
        let (out, _) = run(&[(p, z, b2, 0.06, 50.0, 0.5, 18, [false, false])], 3.0);
        assert!(out[59] < 50.6, "spike {}", out[59]);
        assert!((out[10] - 50.0).abs() < 1e-9 && (out[110] - 50.0).abs() < 1e-9);
    }

    /// A steep residential street that the ground itself has is not flattened.
    #[test]
    fn natural_steep_street_is_kept() {
        let p = line(0.0, 0.0, 2.0, 0.0, 100);
        let z: Vec<f64> = (0..100).map(|i| 0.20 * 2.0 * i as f64).collect(); // 20 %
        let (out, st) = run(&[(p, z.clone(), z.clone(), 0.12, 25.0, 1.0, 6, [false, false])], 3.0);
        for i in 0..100 {
            assert!((out[i] - z[i]).abs() < 1e-6, "i {} {} {}", i, out[i], z[i]);
        }
        assert_eq!(st.changed_pts, 0);
    }

    /// An underpass dip (designed: far under its natural height) keeps its depth.
    #[test]
    fn designed_dip_is_kept() {
        let p = line(0.0, 0.0, 2.0, 0.0, 200);
        let base = vec![20.0; 200];
        let mut z = base.clone();
        for i in 60..140 {
            let t = (i - 60) as f64 / 80.0; // 160 m long, 6 m deep: 11.8 % at the steepest
            z[i] = 20.0 - 6.0 * (std::f64::consts::PI * t).sin().powi(2);
        }
        let (out, _) = run(&[(p, z.clone(), base, 0.09, 40.0, 0.75, 14, [false, false])], 3.0);
        assert!(out[100] < 14.1, "dip {}", out[100]);
    }

    fn max_bend(z: &[f64], step: f64, over: f64) -> f64 {
        let k = (over / step) as usize;
        (k..z.len() - k).map(|i| ((z[i + k] - z[i]) - (z[i] - z[i - k])).abs() / over).fold(0.0, f64::max)
    }

    /// Parts of equal-ranked roads ending at one point at two heights: they meet at one height between them (by
    /// length), and each eases back to its own profile without a crease.
    #[test]
    fn a_node_of_equal_ranks_meets_at_one_height_smoothly() {
        let a = line(0.0, 0.0, 2.0, 0.0, 300);
        let b = line(0.0, 0.0, -2.0, 0.0, 300);
        let c = line(0.0, 0.0, 0.0, 2.0, 200);
        let (z, _) = run(
            &[(a, vec![302.0; 300], vec![304.0; 300], 0.06, 0.0, 0.0, 16, [false, false]),
              (b, vec![306.0; 300], vec![304.0; 300], 0.06, 0.0, 0.0, 16, [false, false]),
              (c, vec![302.0; 200], vec![304.0; 200], 0.09, 0.0, 0.0, 10, [false, false])],
            3.0,
        );
        assert!((z[0] - 304.0).abs() < 0.02 && (z[300] - 304.0).abs() < 0.02 && (z[600] - 304.0).abs() < 0.02,
                "{} {} {}", z[0], z[300], z[600]);
        for r in [&z[0..300], &z[300..600], &z[600..800]] {
            assert!(max_slope(r, 2.0) <= 0.06, "grade {}", max_slope(r, 2.0));
            assert!(max_bend(r, 2.0, 20.0) <= 0.042, "bend {}", max_bend(r, 2.0, 20.0));
        }
        assert!((z[299] - 302.0).abs() < 1e-9, "the far end stays");
    }

    /// An ease longer than a part goes on through a plain continuation, with no second step at the joint.
    #[test]
    fn an_ease_runs_on_through_the_parts_of_a_road() {
        let main = line(0.0, 0.0, 2.0, 0.0, 200);
        let p1 = line(0.0, 0.0, 0.0, 2.0, 16); // 30 m each
        let p2 = line(0.0, 30.0, 0.0, 2.0, 16);
        let p3 = line(0.0, 60.0, 0.0, 2.0, 100);
        let (z, _) = run(
            &[(main, vec![10.0; 200], vec![0.0; 200], 0.09, 0.0, 0.0, 14, [true, false]),
              (p1, vec![0.0; 16], vec![0.0; 16], 0.12, 0.0, 0.0, 6, [false, false]),
              (p2, vec![0.0; 16], vec![0.0; 16], 0.12, 0.0, 0.0, 6, [false, false]),
              (p3, vec![0.0; 100], vec![0.0; 100], 0.12, 0.0, 0.0, 6, [false, false])],
            3.0,
        );
        assert!((z[200] - 10.0).abs() < 0.02, "{}", z[200]);
        assert!((z[215] - z[216]).abs() < 1e-6 && (z[231] - z[232]).abs() < 1e-6, "joints stay shut");
        let mut r: Vec<f64> = z[200..216].to_vec();
        r.extend_from_slice(&z[217..232]);
        r.extend_from_slice(&z[233..]);
        assert!(max_slope(&r, 2.0) <= 0.125, "grade {}", max_slope(&r, 2.0));
        assert!(max_bend(&r, 2.0, 20.0) <= 0.042, "bend {}", max_bend(&r, 2.0, 20.0));
    }

    /// A step the ground itself has (a natural difference over node_max) stays.
    #[test]
    fn the_datas_own_step_at_a_node_stays() {
        let a = line(0.0, 0.0, 2.0, 0.0, 100);
        let b = line(0.0, 0.0, -2.0, 0.0, 100);
        let (za, zb) = (vec![0.0; 100], vec![8.0; 100]);
        let (z, _) = run(&[(a, za.clone(), za.clone(), 0.12, 0.0, 0.0, 6, [false, false]),
                           (b, zb.clone(), zb.clone(), 0.12, 0.0, 0.0, 6, [false, false])], 3.0);
        assert!((z[100] - z[0]).abs() > 2.0, "{} {}", z[0], z[100]);
    }

    /// A real wall in the data (a 6 m step between two bridge abutments 78 m apart, a ceiling of 5 %) cannot be solved, and the pass says so
    /// instead of flattening a road that has nowhere to go.
    #[test]
    fn unsolvable_is_reported() {
        let p = line(0.0, 0.0, 2.0, 0.0, 40);
        let mut z = vec![0.0; 40];
        for v in z.iter_mut().skip(20) {
            *v = 6.0;
        }
        let base = vec![0.0; 40];
        let (_out, st) = run(&[(p, z, base, 0.05, 0.0, 0.0, 6, [true, true])], 3.0);
        assert!(st.max_viol_after > 0.5, "{:?}", st);
    }
}
