//! roads.TunnelProfiles: the floor heights at the joints of a tunnel network, solved over the whole network at once.
//!
//! A mapped tunnel comes as several parts: one way split where its tags change, at a municipal border, at every
//! junction under the mountain (Tromsø's tunnels with roundabouts inside the hill, Stuttgart's underground junctions,
//! GitHub #490), or clipped by the map cells. Each part was given the lower hull of the ground over it with both its
//! ends at the ground there: a joint deep inside the mountain was put at the mountain's surface, so the tube climbed
//! out of the hill to every joint (GitHub #1160, #1161: "the tunnel is very steep", a long tunnel as two short ones
//! with a road over the ridge between them; Tromsø's underground roundabouts drawn on the mountain top, 80 m over the
//! tunnels they join). Here the network's joints get their heights from the hull of the ground along the longest path
//! between two of its portals, then the next longest path between solved nodes, and so on: a straight tunnel split
//! anywhere gets the floor it would have had in one piece.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

/// The lower convex hull of (s, g) (s ascending) at every s: the floor a tunnel takes under the ground g between its
/// two end points (which the hull passes through).
pub fn lower_hull(s: &[f64], g: &[f64]) -> Vec<f64> {
    let n = s.len();
    let mut floor = vec![0.0f64; n];
    if n == 0 {
        return floor;
    }
    let mut hull: Vec<usize> = Vec::new();
    for i in 0..n {
        while hull.len() >= 2 {
            let (a, b) = (hull[hull.len() - 2], hull[hull.len() - 1]);
            let cross = (s[b] - s[a]) * (g[i] - g[a]) - (g[b] - g[a]) * (s[i] - s[a]);
            if cross <= 0.0 {
                hull.pop();
            } else {
                break;
            }
        }
        hull.push(i);
    }
    let mut k = 0usize;
    for i in 0..n {
        while k + 1 < hull.len() - 1 && s[hull[k + 1]] <= s[i] {
            k += 1;
        }
        let (a, b) = (hull[k], hull[(k + 1).min(hull.len() - 1)]);
        floor[i] = if b == a || s[b] <= s[a] { g[a] } else { g[a] + (g[b] - g[a]) * (s[i] - s[a]) / (s[b] - s[a]) };
    }
    floor
}

#[derive(PartialEq)]
struct Item(f64, usize);
impl Eq for Item {}
impl Ord for Item {
    fn cmp(&self, o: &Self) -> Ordering {
        // a min heap on distance, then node number (deterministic)
        o.0.partial_cmp(&self.0).unwrap_or(Ordering::Equal).then_with(|| o.1.cmp(&self.1))
    }
}
impl PartialOrd for Item {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

struct Net<'a> {
    adj: Vec<Vec<(usize, usize)>>, // node -> (edge, other node)
    ea: &'a [i64],
    starts: &'a [i64],
    s: &'a [f64],
    g: &'a [f64],
    sl: &'a [f64],
    sr: &'a [f64],
}

impl<'a> Net<'a> {
    fn len(&self, e: usize) -> f64 {
        let (a, b) = (self.starts[e] as usize, self.starts[e + 1] as usize);
        if b > a { self.s[b - 1] - self.s[a] } else { 0.0 }
    }

    /// Shortest paths from node k through unknown nodes only (k itself may be known): (dist, prev (edge, node)).
    fn paths(&self, k: usize, known: &[bool]) -> (Vec<f64>, Vec<Option<(usize, usize)>>) {
        let n = self.adj.len();
        let mut dist = vec![f64::INFINITY; n];
        let mut prev: Vec<Option<(usize, usize)>> = vec![None; n];
        dist[k] = 0.0;
        let mut q = BinaryHeap::new();
        q.push(Item(0.0, k));
        while let Some(Item(d, u)) = q.pop() {
            if d > dist[u] {
                continue;
            }
            if u != k && known[u] {
                continue; // (a known node ends a path)
            }
            for &(e, v) in &self.adj[u] {
                if v == u {
                    continue;
                }
                let nd = d + self.len(e);
                if nd < dist[v] {
                    dist[v] = nd;
                    prev[v] = Some((e, u));
                    q.push(Item(nd, v));
                }
            }
        }
        (dist, prev)
    }

    /// The profile along the path ending at node `end` (prev from paths): s, g, the ground beside it on its left and
    /// right (as walked), and the arc at each of its nodes.
    #[allow(clippy::type_complexity)]
    fn profile(&self, end: usize, prev: &[Option<(usize, usize)>])
               -> (Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<(usize, f64)>) {
        let mut steps = Vec::new(); // (edge, from node) from the end back to the start
        let mut u = end;
        while let Some((e, p)) = prev[u] {
            steps.push((e, p));
            u = p;
        }
        steps.reverse();
        let (mut ss, mut gs, mut nodes) = (Vec::new(), Vec::new(), vec![(u, 0.0)]);
        let (mut ls, mut rs) = (Vec::new(), Vec::new());
        let mut off = 0.0;
        for (e, from) in steps {
            let (a, b) = (self.starts[e] as usize, self.starts[e + 1] as usize);
            let fwd = self.ea[e] as usize == from;
            let l = self.len(e);
            let idx: Vec<usize> = if fwd { (a..b).collect() } else { (a..b).rev().collect() };
            for (m, &i) in idx.iter().enumerate() {
                if m == 0 && !ss.is_empty() {
                    continue; // (the joint's sample: the previous edge's last)
                }
                let si = if fwd { self.s[i] - self.s[a] } else { self.s[b - 1] - self.s[i] };
                ss.push(off + si);
                gs.push(self.g[i]);
                let (l, r) = (self.sl.get(i).copied().unwrap_or(f64::NAN), self.sr.get(i).copied().unwrap_or(f64::NAN));
                ls.push(if fwd { l } else { r });
                rs.push(if fwd { r } else { l });
            }
            off += l;
            let to = self.adj[from].iter().find(|&&(e2, _)| e2 == e).map(|&(_, v)| v).unwrap_or(from);
            nodes.push((to, off));
        }
        (ss, gs, ls, rs, nodes)
    }
}

/// The floor of a tunnel line (arc s, its ends' heights in g[0] and g[last]): the lower hull of g, lowered where the map
/// has the tunnel under flat ground (the ground beside it, side_l / side_r, within flat_m of the ground over it,
/// cover_g) with less than open_cover over that hull for min_run m or more, to `need` under that ground, eased in and
/// out at `grade` and no lower than `grade` from either end. A city's cut-and-cover tunnel under level streets (Perth's
/// Graham Farmer Freeway under Northbridge, 1.6 km, GitHub #464 #1018; Stuttgart's B14, GitHub #490; I-395 under
/// Washington): the hull of a level street is the street, so the tunnel was an open road through the city at street
/// level, its covered pieces' tubes standing through the streets over them. An open cutting (the ground beside higher:
/// Central Park's sunken transverses) or a hillside (one side higher: a gallery) keeps its hull. need <= 0: the hull.
#[allow(clippy::too_many_arguments)]
pub fn buried_floor(s: &[f64], g: &[f64], gc: &[f64], side_l: &[f64], side_r: &[f64], open_cover: f64, need: f64,
                    grade: f64, flat_m: f64, min_run: f64) -> Vec<f64> {
    buried_floor_flag(s, g, gc, side_l, side_r, open_cover, need, grade, flat_m, min_run, (true, true)).0
}

/// buried_floor and whether it buried any of the line (true: its portals stand where the ramps reach the depth).
/// portals: (start, end) is a portal the ramps climb to; else a point inside the tunnel (a joint, a map cell's clip)
/// that holds its height but no ramp.
#[allow(clippy::too_many_arguments)]
pub fn buried_floor_flag(s: &[f64], g: &[f64], gc: &[f64], side_l: &[f64], side_r: &[f64], open_cover: f64,
                         need: f64, grade: f64, flat_m: f64, min_run: f64, portals: (bool, bool))
                         -> (Vec<f64>, bool) {
    let n = s.len();
    let floor = lower_hull(s, g);
    if n < 3 || !(need > 0.0) || gc.len() != n || side_l.len() != n || side_r.len() != n {
        return (floor, false);
    }
    // the stretches under flat ground the hull leaves shallower than need (open_cover: unused, kept for the callers)
    let cand: Vec<bool> = (0..n)
        .map(|i| {
            let (l, r) = (side_l[i] - gc[i], side_r[i] - gc[i]);
            l.abs() < flat_m && r.abs() < flat_m && gc[i] - floor[i] < need
        })
        .collect();
    let mut target = vec![f64::INFINITY; n];
    let mut any = false;
    let mut i = 0;
    while i < n {
        if !cand[i] {
            i += 1;
            continue;
        }
        let mut j = i;
        while j + 1 < n && cand[j + 1] {
            j += 1;
        }
        if s[j] - s[i] >= min_run {
            for q in i..=j {
                target[q] = gc[q] - need;
            }
            any = true;
        }
        i = j + 1;
    }
    if !any {
        return (floor, false);
    }
    // eased in and out at grade (the lower envelope of the targets' cones), never over the hull
    let mut c = target.clone();
    for q in 1..n {
        c[q] = c[q].min(c[q - 1] + grade * (s[q] - s[q - 1]));
    }
    for q in (0..n - 1).rev() {
        c[q] = c[q].min(c[q + 1] + grade * (s[q + 1] - s[q]));
    }
    let (z0, z1, total) = (floor[0], floor[n - 1], s[n - 1]);
    let mut out = floor.clone();
    for q in 0..n {
        let a = if portals.0 { z0 - grade * s[q] } else { f64::NEG_INFINITY };
        let b = if portals.1 { z1 - grade * (total - s[q]) } else { f64::NEG_INFINITY };
        let low = a.max(b);
        out[q] = floor[q].min(c[q]).max(low);
    }
    // (rounded over BURY_SMOOTH_M: three box passes, the ends held, never over the hull: the target follows every
    // rise of the street over it and the ramps meet it at a kink, crests and dips a car feels at speed)
    for _ in 0..3 {
        let prev = out.clone();
        let mut lo = 0usize;
        let mut hi = 0usize;
        let mut sum = 0.0;
        for q in 0..n {
            while hi < n && s[hi] <= s[q] + BURY_SMOOTH_M {
                sum += prev[hi];
                hi += 1;
            }
            while s[lo] < s[q] - BURY_SMOOTH_M {
                sum -= prev[lo];
                lo += 1;
            }
            out[q] = (sum / (hi - lo) as f64).min(floor[q]);
        }
        out[0] = z0;
        out[n - 1] = z1;
    }
    (out, true)
}

/// m: the half window of buried_floor's rounding passes
const BURY_SMOOTH_M: f64 = 12.0;

/// The floor height at each node of a tunnel network: edges e from node ea[e] to node eb[e] with their ground profile
/// (arc s from 0 at ea's end, ground g) at s[starts[e]..starts[e + 1]], g alike; fixed[n] the height of a portal node
/// (NaN: a joint inside, or a free end). The longest path (through unknown nodes) between two known nodes first: its
/// ground's lower hull through its two ends gives the nodes along it; then the next, until none is left. Then the free
/// ends: a dead end with less than `cover` of ground over the node it is reached from is a portal at its ground (a
/// map's tunnel end with no road mapped on to it, as before), else a closed end at that node's height (a cavern, a
/// chain cut short by the map cells read). NaN where no portal reaches. Pure.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tunnel_chain_nodes<'py>(
    py: Python<'py>,
    n_nodes: usize,
    ea: PyReadonlyArray1<'py, i64>,
    eb: PyReadonlyArray1<'py, i64>,
    starts: PyReadonlyArray1<'py, i64>,
    s: PyReadonlyArray1<'py, f64>,
    g: PyReadonlyArray1<'py, f64>,
    side_l: PyReadonlyArray1<'py, f64>,
    side_r: PyReadonlyArray1<'py, f64>,
    fixed: PyReadonlyArray1<'py, f64>,
    cover: f64,
    bury: PyReadonlyArray1<'py, f64>,
) -> Bound<'py, PyArray1<f64>> {
    let (ea, eb, starts, s, g, fixed) =
        (ea.as_slice().unwrap(), eb.as_slice().unwrap(), starts.as_slice().unwrap(), s.as_slice().unwrap(),
         g.as_slice().unwrap(), fixed.as_slice().unwrap());
    chain_nodes(n_nodes, ea, eb, starts, s, g, side_l.as_slice().unwrap(), side_r.as_slice().unwrap(), fixed, cover,
                bury.as_slice().unwrap())
        .into_pyarray(py)
}

#[allow(clippy::too_many_arguments)]
pub fn chain_nodes(n: usize, ea: &[i64], eb: &[i64], starts: &[i64], s: &[f64], g: &[f64], sl: &[f64], sr: &[f64],
                   fixed: &[f64], cover: f64, bury: &[f64]) -> Vec<f64> {
    // bury: [open_cover, need, grade, flat_m, min_run] for buried_floor (need <= 0 or empty: the hull)
    let portal: Vec<bool> = (0..n).map(|k| fixed.get(k).map_or(false, |v| v.is_finite())).collect();
    let floor_of = |ss: &[f64], gs: &[f64], gc: &[f64], ls: &[f64], rs: &[f64], ends: (bool, bool)| -> Vec<f64> {
        if bury.len() >= 5 {
            buried_floor_flag(ss, gs, gc, ls, rs, bury[0], bury[1], bury[2], bury[3], bury[4], ends).0
        } else {
            lower_hull(ss, gs)
        }
    };
    let mut z: Vec<f64> = (0..n).map(|k| fixed.get(k).copied().unwrap_or(f64::NAN)).collect();
    let mut adj: Vec<Vec<(usize, usize)>> = vec![Vec::new(); n];
    for e in 0..ea.len() {
        let (a, b) = (ea[e] as usize, eb[e] as usize);
        if a >= n || b >= n || starts[e + 1] - starts[e] < 2 {
            continue;
        }
        adj[a].push((e, b));
        if b != a {
            adj[b].push((e, a));
        }
    }
    let net = Net { adj, ea, starts, s, g, sl, sr };
    let mut known: Vec<bool> = z.iter().map(|v| v.is_finite()).collect();
    // the ground at each node (its first sample along an edge)
    let mut node_g = vec![f64::NAN; n];
    for e in 0..ea.len() {
        let (a, b) = (starts[e] as usize, starts[e + 1] as usize);
        if b <= a {
            continue;
        }
        let (na, nb) = (ea[e] as usize, eb[e] as usize);
        if na < n && node_g[na].is_nan() {
            node_g[na] = g[a];
        }
        if nb < n && node_g[nb].is_nan() {
            node_g[nb] = g[b - 1];
        }
    }
    loop {
        // the longest of the shortest paths between two known nodes through unknown ones
        let mut best: Option<(f64, usize, usize, Vec<Option<(usize, usize)>>)> = None;
        for k in 0..n {
            if !known[k] {
                continue;
            }
            let (dist, prev) = net.paths(k, &known);
            for j in 0..n {
                if j == k || !known[j] || !dist[j].is_finite() {
                    continue;
                }
                // only a path through at least one unknown node (or none left to give)
                let mut u = j;
                let mut through = false;
                while let Some((_, p)) = prev[u] {
                    if p != k && !known[p] {
                        through = true;
                    }
                    u = p;
                }
                if !through {
                    continue;
                }
                if best.as_ref().map_or(true, |b| dist[j] > b.0 + 1e-9) {
                    best = Some((dist[j], k, j, prev.clone()));
                }
            }
        }
        if let Some((_d, k, j, prev)) = best {
            let (ss, mut gs, ls, rs, nodes) = net.profile(j, &prev);
            if gs.len() < 2 {
                break;
            }
            let gc = gs.clone();
            let last = gs.len() - 1;
            gs[0] = z[k];
            gs[last] = z[j];
            let f = floor_of(&ss, &gs, &gc, &ls, &rs, (portal[k], portal[j]));
            for &(nd, at) in &nodes {
                if !known[nd] {
                    let i = ss.partition_point(|&v| v < at - 1e-9).min(last);
                    z[nd] = f[i];
                    known[nd] = true;
                }
            }
            continue;
        }
        // no path joins two known nodes: the farthest unknown node from a known one (a dead end)
        let mut far: Option<(f64, usize, usize, Vec<Option<(usize, usize)>>)> = None;
        for k in 0..n {
            if !known[k] {
                continue;
            }
            let (dist, prev) = net.paths(k, &known);
            for j in 0..n {
                if known[j] || !dist[j].is_finite() {
                    continue;
                }
                if far.as_ref().map_or(true, |b| dist[j] > b.0 + 1e-9) {
                    far = Some((dist[j], k, j, prev.clone()));
                }
            }
        }
        let Some((_d, k, j, prev)) = far else { break };
        let (ss, mut gs, ls, rs, nodes) = net.profile(j, &prev);
        if gs.len() < 2 {
            known[j] = true;
            z[j] = node_g[j];
            continue;
        }
        let gc = gs.clone();
        let last = gs.len() - 1;
        gs[0] = z[k];
        let ge = gs[last];
        // shallow: a portal at its ground; deep inside: closed, level with the node it is reached from
        gs[last] = if ge - z[k] < cover { ge } else { z[k] };
        let f = floor_of(&ss, &gs, &gc, &ls, &rs, (portal[k], false));
        for &(nd, at) in &nodes {
            if !known[nd] {
                let i = ss.partition_point(|&v| v < at - 1e-9).min(last);
                z[nd] = f[i];
                known[nd] = true;
            }
        }
    }
    z
}

/// kinds with each open gap shorter than gap_m between two covered runs (inside the tunnel, not at a portal) covered
/// (the lower kind beside it) unless the ground beside it shows a cutting there (both sides trench_m or more over the
/// ground over the line, on average): the map has the road in a tunnel all along, and a gully crossing over it (the
/// DEM down at the road's level for a few metres) is not daylight: the Schöllenen's galleries stood as two short tubes
/// with a 12 m open road between them (GitHub #1160: "should be one long tunnel"); an open cutting between a park's
/// bridges stays open (Central Park's transverses). Pure.
pub fn close_gaps(s: &[f64], gc: &[f64], side_l: &[f64], side_r: &[f64], kinds: &mut [i64], gap_m: f64,
                  trench_m: f64) {
    let n = kinds.len();
    if n < 3 || s.len() != n || gc.len() != n || side_l.len() != n || side_r.len() != n {
        return;
    }
    let mut q = 0;
    while q < n {
        if kinds[q] != 0 {
            q += 1;
            continue;
        }
        let mut e = q;
        while e + 1 < n && kinds[e + 1] == 0 {
            e += 1;
        }
        if q > 0 && e + 1 < n && s[e + 1] - s[q - 1] < gap_m {
            let (mut sum, mut m) = (0.0, 0usize);
            for v in q..=e {
                let d = side_l[v].min(side_r[v]) - gc[v];
                if d.is_finite() {
                    sum += d;
                    m += 1;
                }
            }
            let cut = m > 0 && sum / m as f64 >= trench_m;
            if m > 0 && !cut {
                let kind = kinds[q - 1].min(kinds[e + 1]);
                for v in q..=e {
                    kinds[v] = kind;
                }
            }
        }
        q = e + 1;
    }
}

fn median(mut v: Vec<f64>) -> f64 {
    v.retain(|x| x.is_finite());
    if v.is_empty() {
        return f64::NAN;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { 0.5 * (v[n / 2 - 1] + v[n / 2]) }
}

/// roads.portal_cover: kinds (per sample at arc s; 0 open, 1 low arch, 2 full) with a major road's open run at either
/// end of its tunnel covered (1) from the section it leads into outwards as far as the ground over the floor (cover)
/// stays `shallow` or more: the map's portal is a cut-and-cover box under shallow ground there (Göschenen's Gotthard
/// portal, 2026-10-28), but its portal stands where that ground ends; covered whole (by the run's median), a ramp
/// leaving a street at grade stood as a 5 m box out of the street, across the street it leaves (I-395 in
/// Washington). An open stub left at the end shorter than min_m: the run covered whole as before; a run shorter than
/// min_m with its median cover shallow or more takes the kind it leads into (CA-1's Tom Lantos tube, 2026-11-09). Pure.
#[pyfunction]
pub fn tunnel_portal_cover<'py>(py: Python<'py>, kinds: PyReadonlyArray1<'py, i64>, cover: PyReadonlyArray1<'py, f64>,
                                s: PyReadonlyArray1<'py, f64>, shallow: f64, min_m: f64, run_rule: bool)
                                -> Bound<'py, PyArray1<i64>> {
    portal_cover(kinds.as_slice().unwrap(), cover.as_slice().unwrap(), s.as_slice().unwrap(), shallow, min_m, run_rule)
        .into_pyarray(py)
}

pub fn portal_cover(kinds: &[i64], cover: &[f64], s: &[f64], shallow: f64, min_m: f64, run_rule: bool) -> Vec<i64> {
    let mut k = kinds.to_vec();
    let n = k.len();
    if n == 0 || !k.iter().any(|&v| v > 0) || cover.len() != n || s.len() != n {
        return k;
    }
    for fwd in [true, false] {
        let idx: Vec<usize> = if fwd { (0..n).collect() } else { (0..n).rev().collect() };
        let Some(pos) = idx.iter().position(|&q| k[q] != 0) else { continue };
        if pos == 0 {
            continue;
        }
        let run = &idx[..pos]; // from the end inwards
        let nxt = idx[pos];
        let med = median(run.iter().map(|&q| cover[q]).collect());
        if (s[nxt] - s[run[0]]).abs() < min_m {
            if med >= shallow {
                for &q in run {
                    k[q] = k[nxt];
                }
            }
            continue;
        }
        if !run_rule {
            // (the median rule alone, BNE_TUNNEL_PORTAL_RUN=off: the whole run or none of it)
            if med >= shallow {
                for &q in run {
                    k[q] = 1;
                }
            }
            continue;
        }
        // from the section it leads into outwards: covered while the ground over the floor stays shallow or more
        let m =run.iter().rev().take_while(|&&q| cover[q].is_finite() && cover[q] >= shallow).count();
        if m == 0 {
            continue;
        }
        let first = run.len() - m; // (the outermost covered sample's place in run)
        let stub = (s[run[first]] - s[run[0]]).abs();
        let from = if stub < min_m && med >= shallow { 0 } else { first };
        for &q in &run[from..] {
            k[q] = 1;
        }
    }
    k
}

#[cfg(test)]
mod tests {
    #[test]
    fn portal_cover_from_inside_out() {
        use super::portal_cover;
        let s: Vec<f64> = (0..10).map(|i| i as f64 * 4.0).collect();
        // a ramp out of a street: no ground over its first metres, then rising; the tube from sample 8
        let cover = [0.0, 0.3, 0.8, 1.2, 1.6, 2.0, 2.4, 3.0, 5.0, 6.0];
        let kinds = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1];
        assert_eq!(portal_cover(&kinds, &cover, &s, 1.5, 8.0, true), vec![0, 0, 0, 0, 1, 1, 1, 1, 1, 1]);
        // a box under a meadow all along: covered whole
        let cover = [1.6, 1.7, 1.8, 2.0, 2.0, 2.0, 2.4, 3.0, 5.0, 6.0];
        assert_eq!(portal_cover(&kinds, &cover, &s, 1.5, 8.0, true), vec![1; 10]);
        // a short open stub before a shallow box: covered whole, as before
        let cover = [1.0, 1.6, 1.8, 2.0, 2.0, 2.0, 2.4, 3.0, 5.0, 6.0];
        assert_eq!(portal_cover(&kinds, &cover, &s, 1.5, 8.0, true), vec![1; 10]);
    }

    use super::*;

    fn run(edges: &[(i64, i64, Vec<f64>)], fixed: &[f64]) -> Vec<f64> {
        let (mut ea, mut eb, mut starts, mut s, mut g) = (vec![], vec![], vec![0i64], vec![], vec![]);
        for (a, b, prof) in edges {
            ea.push(*a);
            eb.push(*b);
            for (i, &v) in prof.iter().enumerate() {
                s.push(i as f64 * 10.0);
                g.push(v);
            }
            starts.push(s.len() as i64);
        }
        let nan = vec![f64::NAN; g.len()];
        chain_nodes(fixed.len(), &ea, &eb, &starts, &s, &g, &nan, &nan, fixed, 8.0, &[])
    }

    #[test]
    fn split_tunnel_joint_inside() {
        // a 2-part tunnel under a ridge: portals at 100 and 110, the ridge 300 m high at the joint
        let z = run(&[(0, 1, vec![100.0, 200.0, 300.0]), (1, 2, vec![300.0, 200.0, 110.0])], &[100.0, f64::NAN, 110.0]);
        assert!((z[1] - 105.0).abs() < 1e-9, "{:?}", z);
    }

    #[test]
    fn reversed_part() {
        let z = run(&[(0, 1, vec![100.0, 200.0, 300.0]), (2, 1, vec![110.0, 200.0, 300.0])], &[100.0, f64::NAN, 110.0]);
        assert!((z[1] - 105.0).abs() < 1e-9, "{:?}", z);
    }

    #[test]
    fn junction_inside_and_branch() {
        // a T under the mountain: 0 -(2 edges)- 3, a branch from joint 1 to portal 4
        let z = run(&[(0, 1, vec![0.0, 100.0, 100.0]), (1, 2, vec![100.0, 100.0, 100.0]), (2, 3, vec![100.0, 50.0, 0.0]),
                      (1, 4, vec![100.0, 60.0, 30.0])],
                    &[0.0, f64::NAN, f64::NAN, 0.0, 30.0]);
        assert!(z[1].abs() < 1e-9 && z[2].abs() < 1e-9, "{:?}", z);
    }

    #[test]
    fn gully_gap_closed_cutting_kept() {
        use super::close_gaps;
        let s: Vec<f64> = (0..10).map(|i| i as f64 * 4.0).collect();
        let gc = vec![100.0; 10];
        let mut k = vec![1, 1, 1, 0, 0, 0, 2, 2, 2, 2];
        let level = gc.clone();
        close_gaps(&s, &gc, &level, &level, &mut k, 40.0, 3.0);
        assert_eq!(k, vec![1, 1, 1, 1, 1, 1, 2, 2, 2, 2]);
        let mut k = vec![1, 1, 1, 0, 0, 0, 2, 2, 2, 2];
        let walls = vec![106.0; 10];
        close_gaps(&s, &gc, &walls, &walls, &mut k, 40.0, 3.0);
        assert_eq!(k, vec![1, 1, 1, 0, 0, 0, 2, 2, 2, 2]);
        let mut k = vec![0, 0, 1, 1, 0, 0, 0, 0, 0, 0];    // (a portal's open run is no gap)
        close_gaps(&s, &gc, &level, &level, &mut k, 40.0, 3.0);
        assert_eq!(k, vec![0, 0, 1, 1, 0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn buried_under_flat_ground() {
        use super::buried_floor;
        let s: Vec<f64> = (0..=100).map(|i| i as f64 * 10.0).collect();
        let g = vec![20.0; s.len()];           // a level city, a 1 km tunnel under it
        let flat = g.clone();
        let f = buried_floor(&s, &g, &g, &flat, &flat, 4.0, 8.0, 0.06, 2.0, 40.0);
        assert!((f[0] - 20.0).abs() < 1e-9 && (f[100] - 20.0).abs() < 1e-9);
        assert!((f[50] - 12.0).abs() < 1e-9, "{}", f[50]);         // 8 m under the street in the middle
        assert!((f[5] - 17.0).abs() < 1e-9, "{}", f[5]);           // ramping down at 6 % from the portal
        // an open cutting (the ground beside 6 m higher): the hull
        let cut = vec![26.0; s.len()];
        let f = buried_floor(&s, &g, &g, &cut, &cut, 4.0, 8.0, 0.06, 2.0, 40.0);
        assert!(f.iter().all(|&v| (v - 20.0).abs() < 1e-9));
        // a gallery on a hillside: one side higher
        let f = buried_floor(&s, &g, &g, &cut, &flat, 4.0, 8.0, 0.06, 2.0, 40.0);
        assert!(f.iter().all(|&v| (v - 20.0).abs() < 1e-9));
    }

    #[test]
    fn dead_end_deep_and_shallow() {
        let z = run(&[(0, 1, vec![0.0, 50.0, 80.0])], &[0.0, f64::NAN]);
        assert!(z[1].abs() < 1e-9, "{:?}", z);       // closed, level
        let z = run(&[(0, 1, vec![0.0, 3.0, 4.0])], &[0.0, f64::NAN]);
        assert!((z[1] - 4.0).abs() < 1e-9, "{:?}", z); // a portal at its ground
    }
}
