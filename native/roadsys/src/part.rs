//! G4 SURFACE: the planar partition of the paved area.
//!
//! Strips: per stroke, rows across ALL its boundaries at once (t_0 >= ... >= t_S, net.rs), a row's vertices shared by
//! the faces on both sides (equal mm positions are one vertex). Zones: at every junction node (or cluster of nodes
//! joined by strokes too short for both cut-backs), a ring made of the arms' own end-row vertices and corner curves,
//! triangulated in integer mm (ear clipping + Lawson flips, v3 gore.rs's predicates). Fork noses: the branch stroke's
//! first (last) row IS the trunk's row at the nose (the same vertex indices). Nothing is trimmed, welded or clipped
//! afterwards; a zone ring that is not simple becomes a recorded fallback.
use crate::geom::*;
use crate::net::*;
use crate::prof::Profiles;

#[derive(Clone, Copy, Debug, Default)]
pub struct Tag { pub kind: u8, pub sup: u8, pub piece: u32, pub slot: u16, pub sid: u32 }

#[derive(Default)]
pub struct Mesh {
    pub v: Vec<[i64; 3]>,
    pub t: Vec<[u32; 3]>,
    pub tag: Vec<Tag>,
}

pub struct Row { pub s: f64, pub b: Vec<u32> }     // b: per boundary (S + 1), vertex index

#[derive(Default, Debug, Clone)]
pub struct PartStats {
    pub zones: usize,
    pub clusters: usize,
    pub cluster_cap: usize,
    pub absorbed: usize,
    pub zone_fallback_straight: usize,
    pub zone_fallback_hull: usize,
    pub zone_union: usize,
    pub zone_union_fail: usize,
    pub arm_order_fallback: usize,
    pub zone_fail: usize,
    pub snap_ok: usize,
    pub snap_mismatch: usize,
    pub folds: usize,
    pub fold_guard: usize,
    pub twin_capped: usize,
    pub twin_lane_overlap: usize,
    pub rows: usize,
    pub tris: usize,
    pub verts: usize,
}

pub struct ArmInfo { pub stroke: usize, pub start: bool }

pub struct Layout {
    pub cluster_of: Vec<usize>,          // node -> cluster id (usize::MAX: none)
    pub clusters: Vec<Vec<usize>>,        // nodes
    pub arms: Vec<Vec<ArmInfo>>,          // per cluster
    pub corner_x: Vec<Vec<Option<P2>>>,  // per cluster, per arm k: X between arm k and k+1
    pub centre: Vec<P2>,
    pub stats: PartStats,
}

fn corner_r(net: &Net, a: usize, b: usize) -> f64 {
    let c = net.strokes[a].cls.min(net.strokes[b].cls);
    let c = if c == 0 { net.strokes[a].cls.max(net.strokes[b].cls) } else { c };
    if c >= 1 && c <= 3 { 6.0 } else { 3.5 }
}

/// World points of an arm's leaving-left and leaving-right edges at distances 0, 2, ... up to dmax from its node.
fn arm_edges(st: &Stroke, start: bool, dmax: f64, off: &mut Vec<f64>) -> (Vec<P2>, Vec<P2>) {
    let len = st.len();
    let dmax = dmax.min(len);
    let mut l = Vec::new();
    let mut r = Vec::new();
    let mut d = 0.0;
    let mut q: Vec<P2> = Vec::new();
    loop {
        let s = if start { d } else { len - d };
        st.offsets(s, off);
        st.row_points(s, off, &mut q);
        let (pl, pr) = (q[0], *q.last().unwrap());
        if start { l.push(pl); r.push(pr); } else { l.push(pr); r.push(pl); }
        if d >= dmax { break; }
        d = (d + 2.0).min(dmax);
    }
    (l, r)
}

/// First intersection of polylines a and b: (arc on a, arc on b, point).
fn poly_x(a: &[P2], b: &[P2]) -> Option<(f64, f64, P2)> {
    let mut sa = 0.0;
    for i in 0..a.len().saturating_sub(1) {
        let la = norm(sub(a[i + 1], a[i]));
        let mut sb = 0.0;
        let mut best: Option<(f64, f64, P2)> = None;
        for j in 0..b.len().saturating_sub(1) {
            let lb = norm(sub(b[j + 1], b[j]));
            if let Some((u, v)) = seg_x(a[i], a[i + 1], b[j], b[j + 1]) {
                let c = (sa + u * la, sb + v * lb, lerp(a[i], a[i + 1], u));
                if best.map(|x| c.0 < x.0).unwrap_or(true) { best = Some(c); }
            }
            sb += lb;
        }
        if best.is_some() { return best; }
        sa += la;
    }
    None
}

/// Shoulders that face a parallel carriageway (a twin, a ramp, a frontage road) are capped by the measured gap
/// between the mapped lines less both carriageways' lane extents, split by their shoulder widths: the width comes
/// from the geometry (the design's "measured" width source), never from a clip after the fact.
fn caps(net: &mut Net, stats: &mut PartStats) {
    const CELL: f64 = 32.0;
    const DMAX: f64 = 40.0;
    let mut grid: std::collections::BTreeMap<(i64, i64), Vec<(usize, usize)>> = std::collections::BTreeMap::new();
    for (si, st) in net.strokes.iter().enumerate() {
        for k in 0..st.line.p.len().saturating_sub(1) {
            let (a, b) = (st.line.p[k], st.line.p[k + 1]);
            let (x0, x1) = ((a[0].min(b[0]) / CELL).floor() as i64, (a[0].max(b[0]) / CELL).floor() as i64);
            let (y0, y1) = ((a[1].min(b[1]) / CELL).floor() as i64, (a[1].max(b[1]) / CELL).floor() as i64);
            for gx in x0..=x1 { for gy in y0..=y1 { grid.entry((gx, gy)).or_default().push((si, k)); } }
        }
    }
    let lane_ext = |st: &Stroke, off: &[f64], left: bool| -> f64 {
        let n = st.slots.len();
        if left { let k = (0..n).find(|&k| st.slots[k].kind != K_SHOULDER).unwrap_or(0); off[k] }
        else { let k = (0..n).rev().find(|&k| st.slots[k].kind != K_SHOULDER).unwrap_or(n - 1); -off[k + 1] }
    };
    let mut caps_out: Vec<(usize, usize, Vec<(f64, f64)>)> = Vec::new();
    let mut nbrs_out: Vec<Vec<usize>> = vec![Vec::new(); net.strokes.len()];
    // (PT3 change 5) the measured gap caps parallel carriageways on any support (a collector on the ground beside a
    // motorway deck); only a pair stacked one above the other (a crossing floor between them) is left alone
    let stacked: std::collections::BTreeSet<(usize, usize)> = net.crossings.iter().map(|c| (c.a.min(c.b), c.a.max(c.b))).collect();
    let mut offa = Vec::new();
    let mut offb = Vec::new();
    for (si, a) in net.strokes.iter().enumerate() {
        let len = a.len();
        let n = ((len / 10.0).ceil() as usize).max(1);
        let ns = a.slots.len();
        let mut capl: Vec<(f64, f64)> = Vec::new();
        let mut capr: Vec<(f64, f64)> = Vec::new();
        let mut scl: Vec<(f64, f64)> = Vec::new();
        let (mut any_l, mut any_r, mut any_s) = (false, false, false);
        for k in 0..=n {
            let s = len * k as f64 / n as f64;
            let (p, t) = a.line.at(s);
            let nrm = [-t[1], t[0]];
            let sup_a = a.support_at(s).kind;
            a.offsets(s, &mut offa);
            // nearest lateral parallel neighbour on each side: (d, stroke, its station)
            let mut best: [Option<(f64, usize, f64)>; 2] = [None, None];
            let (gx, gy) = ((p[0] / CELL).floor() as i64, (p[1] / CELL).floor() as i64);
            for dx in -1..=1 { for dy in -1..=1 {
                let Some(list) = grid.get(&(gx + dx, gy + dy)) else { continue };
                for &(sj, kj) in list {
                    if sj == si { continue; }
                    let b = &net.strokes[sj];
                    if b.absorbed { continue; }
                    let (q0, q1) = (b.line.p[kj], b.line.p[kj + 1]);
                    let ab = sub(q1, q0);
                    let l2 = dot(ab, ab);
                    if l2 <= 1e-9 { continue; }
                    let u = clampf(dot(sub(p, q0), ab) / l2, 0.0, 1.0);
                    let q = lerp(q0, q1, u);
                    let v = sub(q, p);
                    let dd = norm(v);
                    if dd < 0.5 || dd > DMAX { continue; }
                    if absf(dot(v, t)) > 0.35 * dd { continue; }
                    let tb = scale(ab, 1.0 / l2.sqrt());
                    if absf(dot(tb, t)) < 0.85 { continue; }
                    let sb = b.line.s[kj] + (b.line.s[kj + 1] - b.line.s[kj]) * u;
                    if b.support_at(sb).kind != sup_a && stacked.contains(&(si.min(sj), si.max(sj))) { continue; }
                    let side = if dot(v, nrm) > 0.0 { 0 } else { 1 };
                    if best[side].map(|x| dd < x.0).unwrap_or(true) { best[side] = Some((dd, sj, sb)); }
                }
            }}
            let mut fac: f64 = 1.0;
            for side in 0..2 {
                let left = side == 0;
                let ka = if left { 0 } else { ns - 1 };
                let sh_a = a.slots[ka].prog.at(s).max(0.0);
                let mut cap = sh_a;
                if let Some((dd, sj, sb)) = best[side] {
                    nbrs_out[si].push(sj);
                    let b = &net.strokes[sj];
                    b.offsets(sb, &mut offb);
                    let (pb, tbv) = b.line.at(sb);
                    let a_on_b_left = cross(tbv, sub(p, pb)) > 0.0;
                    let eb = lane_ext(b, &offb, a_on_b_left);
                    let kb = if a_on_b_left { 0 } else { b.slots.len() - 1 };
                    let sh_b = b.slots[kb].prog.at(sb).max(0.0);
                    let ea = lane_ext(a, &offa, left);
                    let r = dd - ea - eb;
                    if r < -0.05 {
                        // lanes wider than the mapped spacing allows: both carriageways' lanes scaled to fit (>= 0.75)
                        let f = (ea + eb + r) / (ea + eb).max(1e-6);
                        if f < 0.75 { stats.twin_lane_overlap += 1; }
                        if f < fac { fac = f.max(0.75); }
                    }
                    let c = if sh_a + sh_b > 0.0 { r.max(0.0) * sh_a / (sh_a + sh_b) } else { 0.0 };
                    if c < cap - 1e-3 { cap = c; if left { any_l = true; } else { any_r = true; } stats.twin_capped += 1; }
                }
                if left { capl.push((s, cap)); } else { capr.push((s, cap)); }
            }
            if fac < 1.0 { any_s = true; }
            scl.push((s, fac));
        }
        if any_l { caps_out.push((si, 0, capl)); }
        if any_r { caps_out.push((si, ns - 1, capr)); }
        if any_s { caps_out.push((si, usize::MAX, scl)); }
    }
    for (si, k, c) in caps_out { if k == usize::MAX { net.strokes[si].lane_scale = c; } else { net.strokes[si].slots[k].cap = c; } }
    for (si, mut v) in nbrs_out.into_iter().enumerate() { v.sort(); v.dedup(); net.strokes[si].cap_nbrs = v; }
}

/// The cluster's arms (stroke ends) in counter-clockwise order along the boundary of the cluster: at every node its
/// arms and its internal strokes (absorbed, joining two of the cluster's nodes) sorted by their leaving direction;
/// from the arm with the smallest stable id, the next item counter-clockwise at the node: an arm is the next arm, an
/// internal stroke is followed to its other node (and the walk goes on after it there). Arms the walk misses (an
/// island inside a loop of internal strokes) follow by their angle about the centre (recorded).
fn boundary_order(net: &Net, nodes: &[usize], arms: &[(f64, usize, bool)], internal: &[usize]) -> (Vec<(f64, usize, bool)>, bool) {
    if arms.len() <= 2 && internal.is_empty() {
        let mut v = arms.to_vec();
        v.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap().then((net.strokes[x.1].uid, x.2).cmp(&(net.strokes[y.1].uid, y.2))));
        return (rotate_min(net, v), false);
    }
    #[derive(Clone, Copy, PartialEq)]
    enum It { Arm(usize), Int(usize, bool) }
    let leave = |si: usize, start: bool| -> (P2, usize) {
        let st = &net.strokes[si];
        let node = if start { st.n0 } else { st.n1 };
        let d = if start { 6.0f64.min(st.len()) } else { (st.len() - 6.0).max(0.0) };
        let (p, _) = st.line.at(d);
        (sub(p, net.nodes[node].p), node)
    };
    let mut items: std::collections::BTreeMap<usize, Vec<(f64, u64, u8, It)>> = std::collections::BTreeMap::new();
    for (k, &(_, si, a)) in arms.iter().enumerate() {
        let (d, node) = leave(si, a);
        items.entry(node).or_default().push((pseudo_angle(d), net.strokes[si].uid, a as u8, It::Arm(k)));
    }
    for &si in internal {
        for start in [true, false] {
            let (d, node) = leave(si, start);
            items.entry(node).or_default().push((pseudo_angle(d), net.strokes[si].uid, 2 + start as u8, It::Int(si, start)));
        }
    }
    for v in items.values_mut() { v.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap().then((x.1, x.2).cmp(&(y.1, y.2)))); }
    let _ = nodes;
    // (no arm leaves the cluster: a closed loop, or a read cut round it: nothing to order, recorded as a fallback)
    let Some(k0) = (0..arms.len()).min_by_key(|&k| (net.strokes[arms[k].1].uid, arms[k].2)) else { return (arms.to_vec(), true) };
    let node0 = leave(arms[k0].1, arms[k0].2).1;
    let mut at = (node0, items[&node0].iter().position(|x| x.3 == It::Arm(k0)).unwrap());
    let mut order = vec![k0];
    let total: usize = items.values().map(|v| v.len()).sum();
    let mut seen = vec![false; arms.len()];
    seen[k0] = true;
    for _ in 0..4 * total + 4 {
        let list = &items[&at.0];
        let i = (at.1 + 1) % list.len();
        match list[i].3 {
            It::Arm(k) => {
                if k == k0 { break; }
                if !seen[k] { seen[k] = true; order.push(k); }
                at = (at.0, i);
            }
            It::Int(si, start) => {
                let st = &net.strokes[si];
                let other = if start { st.n1 } else { st.n0 };
                let Some(ol) = items.get(&other) else { break };
                let Some(j) = ol.iter().position(|x| x.3 == It::Int(si, !start)) else { break };
                at = (other, j);
            }
        }
    }
    let mut out: Vec<(f64, usize, bool)> = order.iter().map(|&k| arms[k]).collect();
    let missed: Vec<(f64, usize, bool)> = (0..arms.len()).filter(|&k| !seen[k]).map(|k| arms[k]).collect();
    let fell = !missed.is_empty();
    if fell {
        // insert each missed arm after the walked arm that precedes it by angle about the centre
        for a in missed {
            let pos = out.iter().position(|x| x.0 > a.0).unwrap_or(out.len());
            out.insert(pos, a);
        }
    }
    (out, fell)
}

fn rotate_min(net: &Net, mut v: Vec<(f64, usize, bool)>) -> Vec<(f64, usize, bool)> {
    if v.is_empty() { return v; }
    let k = (0..v.len()).min_by_key(|&k| (net.strokes[v[k].1].uid, v[k].2)).unwrap();
    v.rotate_left(k);
    v
}

pub fn layout(net: &mut Net) -> Layout {
    let nn = net.nodes.len();
    let ns = net.strokes.len();
    let mut ends: Vec<Vec<(usize, bool)>> = vec![Vec::new(); nn];
    for (si, st) in net.strokes.iter().enumerate() {
        ends[st.n0].push((si, true));
        ends[st.n1].push((si, false));
    }
    let mut stats = PartStats::default();
    caps(net, &mut stats);
    let mut off = Vec::new();
    // fork branch cut stations: the branch's own arc at the nose (fork_event's gore-from-geometry samples)
    for si in 0..ns {
        let len = net.strokes[si].len();
        if let Some(sn) = net.strokes[si].snap0 { net.strokes[si].cut0 = clampf(sn.tb, 0.0, (len - 0.5).max(0.0)); }
        if let Some(sn) = net.strokes[si].snap1 { net.strokes[si].cut1 = clampf(sn.tb, 0.0, (len - 0.5).max(0.0)); }
    }
    // junction nodes: >= 2 stroke ends, not a fork event (a fork's branch end is its snap)
    let is_junction = |n: usize, ends: &Vec<Vec<(usize, bool)>>| -> bool {
        if net.fork_at[n].is_some() { return false; }
        // a cut joint (net.rs joints): the strokes continue each other through the node, no zone
        if net.cut[n] && ends[n].iter().any(|&(si, a)| a && net.strokes[si].snap0.map(|x| x.joint).unwrap_or(false)) { return false; }
        let e: Vec<&(usize, bool)> = ends[n].iter().filter(|&&(si, a)| {
            let st = &net.strokes[si];
            !(a && st.snap0.is_some() && false) && !(!a && st.snap1.is_some() && false)
        }).collect();
        e.len() >= 2
    };
    let mut cl: Vec<usize> = (0..nn).collect();
    fn find(p: &mut Vec<usize>, x: usize) -> usize { let mut r = x; while p[r] != r { r = p[r]; } let mut y = x; while p[y] != r { let n = p[y]; p[y] = r; y = n; } r }
    let junction: Vec<bool> = (0..nn).map(|n| is_junction(n, &ends)).collect();
    let mut cut = vec![(0.0f64, 0.0f64); ns];
    let mut absorbed = vec![false; ns];
    let mut short = vec![false; ns];
    let mut result_arms: Vec<Vec<ArmInfo>> = Vec::new();
    let mut result_x: Vec<Vec<Option<P2>>> = Vec::new();
    let mut result_nodes: Vec<Vec<usize>> = Vec::new();
    let mut result_centre: Vec<P2> = Vec::new();
    let mut cluster_of = vec![usize::MAX; nn];
    for pass in 0..3 {
        // clusters as they stand
        let mut groups: std::collections::BTreeMap<usize, Vec<usize>> = std::collections::BTreeMap::new();
        for n in 0..nn { if junction[n] { let r = find(&mut cl, n); groups.entry(r).or_default().push(n); } }
        for c in cut.iter_mut() { *c = (0.0, 0.0); }
        result_arms.clear(); result_x.clear(); result_nodes.clear(); result_centre.clear();
        for v in cluster_of.iter_mut() { *v = usize::MAX; }
        for (_r, nodes) in &groups {
            let cid = result_arms.len();
            for &n in nodes { cluster_of[n] = cid; }
            let mut centre = [0.0, 0.0];
            for &n in nodes { centre = add(centre, net.nodes[n].p); }
            centre = scale(centre, 1.0 / nodes.len() as f64);
            // arms: stroke ends at the cluster's nodes, strokes with both ends inside are absorbed
            let mut arms: Vec<(f64, usize, bool)> = Vec::new();
            for &n in nodes {
                for &(si, a) in &ends[n] {
                    let st = &net.strokes[si];
                    let other = if a { st.n1 } else { st.n0 };
                    // absorbed: a stroke between two nodes of one cluster that was too short for both cut-backs
                    // (it merged them), never a loop
                    if junction[other] && other != n && find(&mut cl, other) == find(&mut cl, n) && nodes.len() > 1
                        && (short[si] || st.len() < 12.0) {
                        absorbed[si] = true;
                        continue;
                    }
                    let d = if a { 6.0f64.min(st.len()) } else { (st.len() - 6.0).max(0.0) };
                    let (p, _t) = st.line.at(d);
                    let dir = sub(p, centre);
                    arms.push((pseudo_angle(dir), si, a));
                }
            }
            // (PT3 change 4) the arms in the order of a walk round the cluster's boundary, not by their angle about
            // the cluster's centre (an elongated cluster's arms sorted about its centroid interleave)
            let internal: Vec<usize> = { let mut v: Vec<usize> = nodes.iter().flat_map(|&n| ends[n].iter().map(|x| x.0))
                .filter(|&si| absorbed[si] && nodes.contains(&net.strokes[si].n0) && nodes.contains(&net.strokes[si].n1)).collect(); v.sort(); v.dedup(); v };
            let (walked, fell_back) = boundary_order(net, nodes, &arms, &internal);
            if fell_back { stats.arm_order_fallback += 1; }
            arms = walked;
            let m = arms.len();
            let edges: Vec<(Vec<P2>, Vec<P2>)> = arms.iter().map(|&(_, si, a)| arm_edges(&net.strokes[si], a, 60.0, &mut off)).collect();
            let mut xs: Vec<Option<P2>> = vec![None; m];
            let mut want = vec![1.0f64; m];
            if m >= 2 {
                for k in 0..m {
                    let j = (k + 1) % m;
                    if m == 2 && k == 1 && arms.len() == 2 { /* both corners of a 2-arm zone */ }
                    if let Some((ua, ub, x)) = poly_x(&edges[k].0, &edges[j].1) {
                        let r = corner_r(net, arms[k].1, arms[j].1);
                        let (ua, ub) = if nodes.len() > 1 { (ua, ub) } else { (ua, ub) };
                        if ua + r > want[k] { want[k] = ua + r; }
                        if ub + r > want[j] { want[j] = ub + r; }
                        xs[k] = Some(x);
                    }
                }
            }
            // the zone's skeleton ring (each arm's end-row ends, each corner's curve midpoint) must be simple:
            // where two of its edges cross, the arms owning them are cut back 4 m more (at most 6 rounds)
            if m >= 2 {
                for _round in 0..6 {
                    let mut pts: Vec<P2> = Vec::new();
                    let mut owner: Vec<(usize, usize)> = Vec::new();   // edge i (pts[i] -> pts[i+1]) owners
                    let ends: Vec<(P2, P2)> = arms.iter().enumerate().map(|(k, &(_, si, a))| {
                        let st = &net.strokes[si];
                        let d = want[k].min(st.len());
                        let sst = if a { d } else { st.len() - d };
                        st.offsets(sst, &mut off);
                        let mut q: Vec<P2> = Vec::new();
                        st.row_points(sst, &off, &mut q);
                        let (pl, pr) = (q[0], *q.last().unwrap());
                        if a { (pr, pl) } else { (pl, pr) }     // (leaving-right, leaving-left)
                    }).collect();
                    for k in 0..m {
                        let j = (k + 1) % m;
                        pts.push(ends[k].0); owner.push((k, k));
                        pts.push(ends[k].1); owner.push((k, j));
                        if let Some(x) = xs[k] {
                            pts.push(add(add(scale(ends[k].1, 0.25), scale(x, 0.5)), scale(ends[j].0, 0.25)));
                            owner.push((k, j));
                        }
                    }
                    let np = pts.len();
                    let mut bad: Vec<usize> = Vec::new();
                    for i in 0..np {
                        for jx in (i + 2)..np {
                            if i == 0 && jx == np - 1 { continue; }
                            if seg_x(pts[i], pts[(i + 1) % np], pts[jx], pts[(jx + 1) % np]).is_some() {
                                bad.push(owner[i].0); bad.push(owner[i].1); bad.push(owner[jx].0); bad.push(owner[jx].1);
                            }
                        }
                    }
                    if bad.is_empty() { break; }
                    bad.sort(); bad.dedup();
                    let mut grew = false;
                    for k in bad {
                        let st = &net.strokes[arms[k].1];
                        if want[k] + 4.0 <= st.len() { want[k] += 4.0; grew = true; }
                    }
                    if !grew { break; }
                }
            }
            for (k, &(_, si, a)) in arms.iter().enumerate() {
                let st = &net.strokes[si];
                let c = want[k].min(st.len());
                if a { cut[si].0 = cut[si].0.max(c); } else { cut[si].1 = cut[si].1.max(c); }
            }
            result_arms.push(arms.iter().map(|&(_, si, a)| ArmInfo { stroke: si, start: a }).collect());
            result_x.push(xs);
            result_nodes.push(nodes.clone());
            result_centre.push(centre);
        }
        if pass == 2 { break; }
        // merge clusters joined by a stroke too short for both cut-backs (capped)
        let mut merged = false;
        for si in 0..ns {
            if absorbed[si] { continue; }
            let st = &net.strokes[si];
            if !(junction[st.n0] && junction[st.n1]) { continue; }
            let (c0, c1) = cut[si];
            if c0 + c1 <= st.len() - 1.0 { continue; }
            let (ra, rb) = (find(&mut cl, st.n0), find(&mut cl, st.n1));
            if ra == rb { if st.n0 != st.n1 { short[si] = true; } continue; }
            let size = (0..nn).filter(|&n| junction[n] && { let r = find(&mut cl, n); r == ra || r == rb }).count();
            if size > 12 || st.len() > 100.0 { stats.cluster_cap += 1; continue; }
            let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) };
            cl[hi] = lo;
            short[si] = true;
            merged = true;
        }
        if !merged { break; }
    }
    for si in 0..ns {
        let st = &mut net.strokes[si];
        if st.snap0.is_none() { st.cut0 = cut[si].0; }
        if st.snap1.is_none() { st.cut1 = cut[si].1; }
        st.absorbed = absorbed[si];
        if st.cut0 + st.cut1 > st.len() - 0.5 && !st.absorbed {
            // overlapping cut-backs that could not merge: keep a 0.5 m strip, the zones meet there (recorded)
            let k = (st.len() - 0.5).max(0.0) / (st.cut0 + st.cut1).max(1e-9);
            st.cut0 *= k; st.cut1 *= k;
        }
    }
    stats.absorbed = absorbed.iter().filter(|&&a| a).count();
    stats.zones = result_arms.iter().filter(|a| a.len() >= 2).count();
    stats.clusters = result_nodes.iter().filter(|n| n.len() > 1).count();
    // branch strokes' gore-side shoulder starts at zero width at the snap (it opens over 30 m)
    for si in 0..ns {
        for at_start in [true, false] {
            let sn = if at_start { net.strokes[si].snap0 } else { net.strokes[si].snap1 };
            let Some(sn) = sn else { continue };
            if sn.joint { continue; }
            let right_side_trunk = sn.k1 + 1 == net.strokes[sn.trunk].slots.len()
                || net.strokes[sn.trunk].slots[sn.k1].kind == K_SHOULDER;
            let st = &mut net.strokes[si];
            let n = st.slots.len();
            // the gore side: a right branch's left shoulder; a left branch's right shoulder
            let k = if right_side_trunk { 0 } else { n - 1 };
            if st.slots[k].kind != K_SHOULDER { continue; }
            let w = st.slots[k].prog.at(if at_start { st.cut0 } else { st.len() - st.cut1 });
            if at_start {
                let a = st.cut0;
                st.slots[k].prog = Prog { w0: 0.0, steps: { let mut v = vec![(a, a + 30.0, w)]; v.extend(st.slots[k].prog.steps.iter().copied().filter(|x| x.0 > a + 30.0)); v } };
            } else {
                let b = st.len() - st.cut1;
                st.slots[k].prog.steps.push((b - 30.0, b, -w));
            }
        }
    }
    Layout { cluster_of, clusters: result_nodes, arms: result_arms, corner_x: result_x, centre: result_centre, stats }
}

/// Row stations of a stroke in [cut0, len - cut1].
fn stations(st: &Stroke) -> Vec<f64> {
    let (a, b) = (st.cut0, st.len() - st.cut1);
    let mut v: Vec<f64> = vec![a, b];
    for &s in &st.line.s { if s > a && s < b { v.push(s); } }
    for &s in &st.stations { if s > a && s < b { v.push(s); } }
    for u in &st.sup { for s in [u.s0, u.s1] { if s > a && s < b { v.push(s); } } }
    for k in &st.slots { for s in [k.alive.0, k.alive.1] { if s.is_finite() && s > a && s < b { v.push(s); } } }
    v.sort_by(|x, y| x.partial_cmp(y).unwrap());
    v.dedup_by(|x, y| (*x - *y) < 0.05);
    // fill: 2 m inside events, else 10 m
    let mut out = Vec::with_capacity(v.len() * 2);
    for k in 0..v.len() {
        out.push(v[k]);
        if k + 1 < v.len() {
            let (s0, s1) = (v[k], v[k + 1]);
            let mid = (s0 + s1) / 2.0;
            let dense = st.dense.iter().any(|&(x, y)| mid >= x && mid <= y);
            let step = if dense { 2.0 } else { 10.0 };
            let n = ((s1 - s0) / step).ceil() as usize;
            for i in 1..n { out.push(s0 + (s1 - s0) * i as f64 / n as f64); }
        }
    }
    out
}

pub fn crossfall(st: &Stroke, s: f64, off: &[f64], sup: u8) -> Vec<f64> {
    let cf = if sup == SUP_STRUCT { 0.025 } else { 0.02 };
    let n = off.len();
    if !st.oneway {
        // crown at the boundary between the two directions
        let mut kc = 0;
        for k in 0..st.slots.len() { if st.slots[k].group == 1 { kc = k + 1; } }
        if st.lht { kc = 0; for k in 0..st.slots.len() { if st.slots[k].group == 0 { kc = k + 1; } } }
        let tc = off[kc.min(n - 1)];
        let _ = s;
        return off.iter().map(|&t| -cf * absf(t - tc)).collect();
    }
    let tm = (off[0] + off[n - 1]) / 2.0;
    let sgn = if st.lht { -1.0 } else { 1.0 };
    off.iter().map(|&t| sgn * cf * (t - tm)).collect()
}

pub struct Built { pub mesh: Mesh, pub rows: Vec<Vec<Row>>, pub stats: PartStats, pub stroke_cpu: Vec<f64>, pub zone_cpu: Vec<f64>, pub zone_piece: Vec<u32> }

/// Strips of the selected strokes (all when None) and the zones of the selected clusters.
pub fn mesh(net: &Net, lay: &Layout, prof: &Profiles, sel: Option<&[bool]>) -> Built {
    let ns = net.strokes.len();
    let mut m = Mesh::default();
    let mut rows: Vec<Vec<Row>> = (0..ns).map(|_| Vec::new()).collect();
    let mut stats = lay.stats.clone();
    let mut stroke_cpu = vec![0.0; ns];
    let mut off = Vec::new();
    // pass 1: rows (vertices) of every stroke, branches after their trunks (snaps read the trunk's rows)
    let mut order: Vec<usize> = (0..ns).collect();
    // (both ends' snaps: a branch's nose and a cut joint; at most 8 deep, a loop of snaps stops there)
    fn depth_of(net: &Net, si: usize, d: usize) -> usize {
        if d >= 8 { return d; }
        let st = &net.strokes[si];
        let mut best = d;
        for sn in [st.snap0, st.snap1].into_iter().flatten() { best = best.max(depth_of(net, sn.trunk, d + 1)); }
        best
    }
    let depth = |si: usize| depth_of(net, si, 0);
    order.sort_by_key(|&si| (depth(si), si));
    let want = |si: usize| -> bool {
        match sel { None => true, Some(v) => v[si] }
    };
    // the strokes a selected stroke snaps to are needed too
    let mut need = vec![false; ns];
    fn mark_need(net: &Net, si: usize, need: &mut Vec<bool>, d: usize) {
        if need[si] && d > 0 { return; }
        need[si] = true;
        if d >= 8 { return; }
        let st = &net.strokes[si];
        for sn in [st.snap0, st.snap1].into_iter().flatten() { mark_need(net, sn.trunk, need, d + 1); }
    }
    for si in 0..ns { if want(si) { mark_need(net, si, &mut need, 0); } }
    // zones need their arms' rows
    let mut zone_want = vec![false; lay.arms.len()];
    for (c, arms) in lay.arms.iter().enumerate() {
        if arms.len() < 2 { continue; }
        if sel.is_none() || arms.iter().any(|a| want(a.stroke)) { zone_want[c] = true; for a in arms { mark_need(net, a.stroke, &mut need, 0); } }
    }
    for &si in &order {
        if !need[si] { continue; }
        let st = &net.strokes[si];
        if st.absorbed { continue; }
        let t0 = crate::cpu_now();
        let stn = stations(st);
        let mut prev_xy: Option<[i64; 2]>;
        let mut snap_delta: Vec<(f64, f64)> = Vec::new();
        let mut prev_row: Option<(f64, P2, Vec<P2>)> = None;     // (station, line point, boundary points)
        let mut ts: Vec<f64> = Vec::new();
        let snap_rows = |sn: &Snap, rows: &Vec<Vec<Row>>| -> Option<Vec<u32>> {
            let tr = &rows[sn.trunk];
            let r = tr.iter().find(|r| absf(r.s - sn.s) < 0.01)?;
            let mut v: Vec<u32> = r.b[sn.k0..=sn.k1 + 1].to_vec();
            v.dedup();
            Some(v)
        };
        for (ri, &s) in stn.iter().enumerate() {
            st.offsets(s, &mut off);
            let (p, n) = st.row_offsets(s, &off, &mut ts);
            // fold guard (PT3 change 3): a boundary that would not advance past the previous row's (a wide ramp's
            // inner edge on a tight curve) is pulled toward the line until it does; the ones inside it follow
            if let Some((s0, p0, q0)) = &prev_row {
                if q0.len() == ts.len() {
                    if let Some(tg) = unit(sub(p, *p0)) {
                        let need = 0.02 * (s - s0);
                        let mut moved = false;
                        for j in 0..ts.len() {
                            if absf(ts[j]) < 0.5 { continue; }
                            let base = dot(sub(p, q0[j]), tg);
                            let gain = ts[j] * dot(n, tg);
                            if base + gain >= need { continue; }
                            let f = if gain < -1e-12 { clampf((need - base) / gain, 0.0, 1.0) } else { 0.0 };
                            ts[j] *= f;
                            moved = true;
                        }
                        if moved { stats.fold_guard += 1; order_offsets(&off, &mut ts); }
                    }
                }
            }
            let pts_row: Vec<P2> = ts.iter().map(|&t| add(p, scale(n, t))).collect();
            prev_row = Some((s, p, pts_row.clone()));
            let sup = st.support_at(s);
            let zc = prof.at(si, s);
            let cf = crossfall(st, s, &off, sup.kind);
            let mut b: Vec<u32> = Vec::with_capacity(off.len());
            prev_xy = None;
            let first = ri == 0;
            let last = ri + 1 == stn.len();
            let snap = if first { st.snap0 } else if last { st.snap1 } else { None };
            let snapped = snap.and_then(|sn| snap_rows(&sn, &rows));
            let mut own: Vec<(P2, f64)> = Vec::new();
            for (k, _t) in off.iter().enumerate() {
                let mut q = pts_row[k];
                // blend away a snap's offset over the first / last 20 m
                if !snap_delta.is_empty() && st.snap0.is_some() {
                    let u = (s - st.cut0) / 20.0;
                    if u < 1.0 { let d = snap_delta[k.min(snap_delta.len() - 1)]; let f = 1.0 - quintic(u); q = add(q, scale([d.0, d.1], f)); }
                }
                own.push((q, zc + cf[k]));
            }
            if st.snap1.is_some() && !last {
                // (merge: blend toward the trunk's row over the last 20 m, computed from the trunk row directly)
                let bend = st.len() - st.cut1;
                let u = (bend - s) / 20.0;
                if u < 1.0 {
                    if let Some(sn) = st.snap1 {
                        if let Some(v) = snap_rows(&sn, &rows) {
                            let mut own_d: Vec<P2> = Vec::new();
                            // the own row at the end
                            let mut oe = Vec::new();
                            st.offsets(bend, &mut oe);
                            let mut qe: Vec<P2> = Vec::new();
                            st.row_points(bend, &oe, &mut qe);
                            let mut ded: Vec<[i64; 2]> = Vec::new();
                            for &q in &qe { let q = mm2(q); if ded.last() != Some(&q) { ded.push(q); } }
                            if ded.len() == v.len() {
                                for (k, &vi) in v.iter().enumerate() {
                                    let tv = m.v[vi as usize];
                                    own_d.push([tv[0] as f64 / 1000.0 - ded[k][0] as f64 / 1000.0, tv[1] as f64 / 1000.0 - ded[k][1] as f64 / 1000.0]);
                                }
                                // map own boundaries to deduped index
                                let mut di = 0usize;
                                let mut lastq: Option<[i64; 2]> = None;
                                for (k, &q) in qe.iter().enumerate() {
                                    let q = mm2(q);
                                    if lastq.is_some() && lastq != Some(q) { di += 1; }
                                    lastq = Some(q);
                                    let f = 1.0 - quintic(u);
                                    if k < own.len() && di < own_d.len() { own[k].0 = add(own[k].0, scale(own_d[di], f)); }
                                }
                            }
                        }
                    }
                }
            }
            if let Some(v) = snapped {
                // the row is the trunk's: map own (deduped) boundaries onto it
                let mut ded: Vec<usize> = Vec::new();   // own boundary -> deduped index
                let mut di = 0usize;
                let mut lastq: Option<[i64; 2]> = None;
                for (q, _z) in &own { let qq = mm2(*q); if lastq.is_some() && lastq != Some(qq) { di += 1; } lastq = Some(qq); ded.push(di); }
                if di + 1 == v.len() {
                    stats.snap_ok += 1;
                    for (k, &d) in ded.iter().enumerate() { b.push(v[d]); let _ = k; }
                    if first {
                        snap_delta = own.iter().enumerate().map(|(k, (q, _))| {
                            let tv = m.v[v[ded[k]] as usize];
                            (tv[0] as f64 / 1000.0 - q[0], tv[1] as f64 / 1000.0 - q[1])
                        }).collect();
                    }
                    rows[si].push(Row { s, b });
                    continue;
                } else { stats.snap_mismatch += 1; }
            }
            for (q, z) in &own {
                let qq = mm2(*q);
                if prev_xy == Some(qq) { let last = *b.last().unwrap(); b.push(last); continue; }
                prev_xy = Some(qq);
                m.v.push([qq[0], qq[1], mm(*z)]);
                b.push((m.v.len() - 1) as u32);
            }
            rows[si].push(Row { s, b });
        }
        stats.rows += rows[si].len();
        // faces (only for the selected strokes)
        if want(si) {
            let rs = &rows[si];
            for r in 0..rs.len().saturating_sub(1) {
                let (ra, rb) = (&rs[r], &rs[r + 1]);
                let sup = st.support_at((ra.s + rb.s) / 2.0);
                for (k, sl) in st.slots.iter().enumerate() {
                    if !(ra.s >= sl.alive.0 - 1e-9 && rb.s <= sl.alive.1 + 1e-9) { continue; }
                    let (a, bb, c, d) = (ra.b[k], ra.b[k + 1], rb.b[k + 1], rb.b[k]);
                    let tag = Tag { kind: sl.kind, sup: sup.kind, piece: piece_of(st.uid), slot: k as u16, sid: if sup.kind == SUP_GROUND { 0 } else { sup.sid } };
                    for tri in [[bb, c, d], [bb, d, a]] {
                        if tri[0] == tri[1] || tri[1] == tri[2] || tri[0] == tri[2] { continue; }
                        let pv = |i: u32| { let v = m.v[i as usize]; [v[0], v[1]] };
                        let o = orient(pv(tri[0]), pv(tri[1]), pv(tri[2]));
                        if o == 0 { continue; }
                        if o < 0 { stats.folds += 1; }
                        m.t.push(tri);
                        m.tag.push(tag);
                    }
                }
            }
        }
        stroke_cpu[si] = crate::cpu_now() - t0;
    }
    // zones
    let mut zone_cpu = vec![0.0; lay.arms.len()];
    let mut zone_piece = vec![0u32; lay.arms.len()];
    for (c, arms) in lay.arms.iter().enumerate() {
        if !zone_want[c] { continue; }
        let t0 = crate::cpu_now();
        let piece = zone_label(net, &lay.clusters[c]);
        let _ = ns;
        zone_piece[c] = piece;
        let mut ring: Vec<u32> = Vec::new();
        let mut ok_rows = true;
        let mut ends: Vec<(u32, u32)> = Vec::new();
        let mut sup_struct = false;
        for a in arms {
            let rs = &rows[a.stroke];
            if rs.is_empty() { ok_rows = false; break; }
            let r = if a.start { &rs[0] } else { rs.last().unwrap() };
            let st = &net.strokes[a.stroke];
            if st.support_at(r.s).kind == SUP_STRUCT { sup_struct = true; }
            let mut v: Vec<u32> = r.b.clone();
            v.dedup();
            if a.start { v.reverse(); }
            ends.push((v[0], *v.last().unwrap()));
            for x in v { if ring.last() != Some(&x) { ring.push(x); } }
            ring.push(u32::MAX);    // corner marker
        }
        if !ok_rows || arms.len() < 2 { stats.zone_fail += 1; continue; }
        // corners
        let mut full: Vec<u32> = Vec::new();
        let mut k_arm = 0;
        let na = arms.len();
        let base_ring = ring.clone();
        let mut straight = false;
        for attempt in 0..2 {
            full.clear();
            k_arm = 0;
            for &x in &base_ring {
                if x != u32::MAX { if full.last() != Some(&x) { full.push(x); } continue; }
                let j = (k_arm + 1) % na;
                let (from, to) = (ends[k_arm].1, ends[j].0);
                if attempt == 0 {
                    if let Some(xp) = lay.corner_x[c].get(k_arm).copied().flatten() {
                        let pa = m.v[from as usize];
                        let pb = m.v[to as usize];
                        let a2 = [pa[0] as f64 / 1000.0, pa[1] as f64 / 1000.0];
                        let b2 = [pb[0] as f64 / 1000.0, pb[1] as f64 / 1000.0];
                        let segs = 4;
                        for i in 1..segs {
                            let u = i as f64 / segs as f64;
                            let q = add(add(scale(a2, (1.0 - u) * (1.0 - u)), scale(xp, 2.0 * u * (1.0 - u))), scale(b2, u * u));
                            let z = pa[2] as f64 * (1.0 - u) + pb[2] as f64 * u;
                            let qq = mm2(q);
                            m.v.push([qq[0], qq[1], z.round() as i64]);
                            full.push((m.v.len() - 1) as u32);
                        }
                    }
                }
                k_arm += 1;
            }
            while full.len() > 1 && full.first() == full.last() { full.pop(); }
            if ring_simple(&m, &full) { break; }
            if attempt == 0 { straight = true; }
        }
        let _ = k_arm;
        if straight { stats.zone_fallback_straight += 1; }
        let tag = Tag { kind: K_ZONE, sup: if sup_struct { SUP_STRUCT } else { SUP_GROUND }, piece, slot: 0, sid: 0 };
        let tris = if ring_simple(&m, &full) { triangulate(&m, &full) } else { None };
        match tris {
            Some(ts) => { for t in ts { m.t.push(t); m.tag.push(tag); } }
            None => {
                // (PT3 change 4) a ring that is not simple: the union it covers (non-zero winding), triangulated
                // face by face of its arrangement; a face that cannot be triangulated is recorded, never filled
                // by a hull
                match triangulate_union(&mut m, &full) {
                    Ok((ts, failed)) => {
                        stats.zone_union += 1;
                        if failed > 0 { stats.zone_union_fail += 1; }
                        for t in ts { m.t.push(t); m.tag.push(tag); }
                    }
                    Err(()) => { stats.zone_union_fail += 1; stats.zone_fail += 1; }
                }
            }
        }
        zone_cpu[c] = crate::cpu_now() - t0;
    }
    stats.tris = m.t.len();
    stats.verts = m.v.len();
    Built { mesh: m, rows, stats, stroke_cpu, zone_cpu, zone_piece }
}

/// A zone's piece label: the top bit and a hash of its cluster's connector ids (stable whatever the build).
fn zone_label(net: &Net, nodes: &[usize]) -> u32 {
    let mut ids: Vec<&str> = nodes.iter().map(|&n| net.node_ids[n].as_str()).collect();
    ids.sort();
    0x8000_0000 | piece_of(uid_of(&ids.join("|"), &[]))
}

/// The triangles covering the union of a closed ring that crosses itself (non-zero winding): the ring's edges split
/// at every crossing (new vertices at the crossings, z interpolated), the faces of that arrangement traced (each
/// on the left of its half-edges), every bounded face whose interior the ring winds round ear-clipped. Returns the
/// triangles and the number of faces that could not be triangulated (recorded by the caller).
fn triangulate_union(m: &mut Mesh, ring: &[u32]) -> Result<(Vec<[u32; 3]>, usize), ()> {
    let n = ring.len();
    if n < 3 { return Err(()); }
    let pt = |m: &Mesh, i: u32| { let v = m.v[i as usize]; [v[0], v[1]] };
    // vertex ids by position (a repeated position is one vertex)
    let mut by_pos: std::collections::BTreeMap<[i64; 2], u32> = std::collections::BTreeMap::new();
    let mut rv: Vec<u32> = Vec::with_capacity(n);
    for &i in ring { let p = pt(m, i); let id = *by_pos.entry(p).or_insert(i); rv.push(id); }
    // split points per ring edge: (param, vertex)
    let mut cuts: Vec<Vec<(f64, u32)>> = vec![Vec::new(); n];
    for i in 0..n {
        let (a, b) = (pt(m, rv[i]), pt(m, rv[(i + 1) % n]));
        if a == b { continue; }
        for j in (i + 1)..n {
            let (c, d) = (pt(m, rv[j]), pt(m, rv[(j + 1) % n]));
            if c == d { continue; }
            if j == i + 1 || (i == 0 && j == n - 1) { continue; }
            if !iseg_cross(a, b, c, d) { continue; }
            let (r, sv) = ([(b[0] - a[0]) as f64, (b[1] - a[1]) as f64], [(d[0] - c[0]) as f64, (d[1] - c[1]) as f64]);
            let den = r[0] * sv[1] - r[1] * sv[0];
            let qp = [(c[0] - a[0]) as f64, (c[1] - a[1]) as f64];
            let add_cut = |m: &mut Mesh, e: usize, u: f64, p: [i64; 2], cuts: &mut Vec<Vec<(f64, u32)>>, by_pos: &mut std::collections::BTreeMap<[i64; 2], u32>, ea: u32, eb: u32| {
                let id = match by_pos.get(&p) { Some(&id) => id, None => {
                    let (za, zb) = (m.v[ea as usize][2] as f64, m.v[eb as usize][2] as f64);
                    m.v.push([p[0], p[1], (za + (zb - za) * u).round() as i64]);
                    let id = (m.v.len() - 1) as u32;
                    by_pos.insert(p, id);
                    id
                } };
                cuts[e].push((u, id));
            };
            if den.abs() > 0.0 {
                let u = (qp[0] * sv[1] - qp[1] * sv[0]) / den;
                let v = (qp[0] * r[1] - qp[1] * r[0]) / den;
                let p = [(a[0] as f64 + r[0] * u).round() as i64, (a[1] as f64 + r[1] * u).round() as i64];
                add_cut(m, i, clampf(u, 0.0, 1.0), p, &mut cuts, &mut by_pos, rv[i], rv[(i + 1) % n]);
                add_cut(m, j, clampf(v, 0.0, 1.0), p, &mut cuts, &mut by_pos, rv[j], rv[(j + 1) % n]);
            } else {
                // collinear overlap: each edge split at the other's ends lying on it
                let l2 = r[0] * r[0] + r[1] * r[1];
                let k2 = sv[0] * sv[0] + sv[1] * sv[1];
                for (p, e, base, dir, ll, ea, eb) in [(c, i, a, r, l2, rv[i], rv[(i + 1) % n]), (d, i, a, r, l2, rv[i], rv[(i + 1) % n]),
                                                     (a, j, c, sv, k2, rv[j], rv[(j + 1) % n]), (b, j, c, sv, k2, rv[j], rv[(j + 1) % n])] {
                    let u = ((p[0] - base[0]) as f64 * dir[0] + (p[1] - base[1]) as f64 * dir[1]) / ll;
                    if u > 0.0 && u < 1.0 { add_cut(m, e, u, p, &mut cuts, &mut by_pos, ea, eb); }
                }
            }
        }
    }
    // the arrangement's undirected edges
    let mut adj: std::collections::BTreeMap<u32, Vec<u32>> = std::collections::BTreeMap::new();
    let mut edges: std::collections::BTreeSet<(u32, u32)> = std::collections::BTreeSet::new();
    let mut ring_pts: Vec<[i64; 2]> = Vec::with_capacity(n);
    for i in 0..n {
        ring_pts.push(pt(m, rv[i]));
        let mut c = cuts[i].clone();
        c.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap().then(x.1.cmp(&y.1)));
        let mut chain = vec![rv[i]];
        for (_u, id) in c { if *chain.last().unwrap() != id { chain.push(id); } }
        let end = rv[(i + 1) % n];
        if *chain.last().unwrap() != end { chain.push(end); }
        for w in chain.windows(2) {
            let (a, b) = (w[0], w[1]);
            if a == b || pt(m, a) == pt(m, b) { continue; }
            let k = if a < b { (a, b) } else { (b, a) };
            if edges.insert(k) { adj.entry(a).or_default().push(b); adj.entry(b).or_default().push(a); }
        }
    }
    // neighbours in counter-clockwise order (exact: half-plane, then cross product)
    for (&v, list) in adj.iter_mut() {
        let o = pt(m, v);
        let half = |p: [i64; 2]| { let (dx, dy) = (p[0] - o[0], p[1] - o[1]); if dy > 0 || (dy == 0 && dx > 0) { 0 } else { 1 } };
        list.sort_by(|&x, &y| {
            let (px, py) = (pt(m, x), pt(m, y));
            let (hx, hy) = (half(px), half(py));
            if hx != hy { return hx.cmp(&hy); }
            let c = orient(o, px, py);
            if c > 0 { std::cmp::Ordering::Less } else if c < 0 { std::cmp::Ordering::Greater } else { x.cmp(&y) }
        });
    }
    // faces on the left of their half-edges: next of (u, v) is (v, w), w the neighbour of v just clockwise of u
    let mut used: std::collections::BTreeSet<(u32, u32)> = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    let mut failed = 0usize;
    let half_edges: Vec<(u32, u32)> = edges.iter().flat_map(|&(a, b)| [(a, b), (b, a)]).collect();
    for &(u0, v0) in &half_edges {
        if used.contains(&(u0, v0)) { continue; }
        let mut face: Vec<u32> = Vec::new();
        let (mut u, mut v) = (u0, v0);
        let mut ok = true;
        for _ in 0..(2 * half_edges.len() + 2) {
            used.insert((u, v));
            face.push(u);
            let list = &adj[&v];
            let k = list.iter().position(|&x| x == u).unwrap();
            let w = list[(k + list.len() - 1) % list.len()];
            u = v;
            v = w;
            if (u, v) == (u0, v0) { break; }
            if used.contains(&(u, v)) { ok = false; break; }
        }
        if !ok || face.len() < 3 { continue; }
        // bounded faces are counter-clockwise
        let mut a2: i128 = 0;
        for i in 0..face.len() { let (p, q) = (pt(m, face[i]), pt(m, face[(i + 1) % face.len()])); a2 += p[0] as i128 * q[1] as i128 - q[0] as i128 * p[1] as i128; }
        if a2 <= 0 { continue; }
        let tris = if ring_simple(m, &face) { triangulate(m, &face) } else { None };
        let Some(tris) = tris else { failed += 1; continue };
        if tris.is_empty() { continue; }
        // the ring's winding round an interior point (3 x the first triangle's centroid, exact)
        let t = tris[0];
        let c3 = { let (a, b, c) = (pt(m, t[0]), pt(m, t[1]), pt(m, t[2])); [a[0] + b[0] + c[0], a[1] + b[1] + c[1]] };
        let mut wn = 0i64;
        for i in 0..n {
            let (a, b) = (ring_pts[i], ring_pts[(i + 1) % n]);
            let (a3, b3) = ([3 * a[0], 3 * a[1]], [3 * b[0], 3 * b[1]]);
            if a3[1] <= c3[1] {
                if b3[1] > c3[1] && orient(a3, b3, c3) > 0 { wn += 1; }
            } else if b3[1] <= c3[1] && orient(a3, b3, c3) < 0 { wn -= 1; }
        }
        if wn != 0 { out.extend(tris); }
    }
    Ok((out, failed))
}

fn ring_simple(m: &Mesh, r: &[u32]) -> bool {
    let n = r.len();
    if n < 3 { return false; }
    let p = |i: usize| { let v = m.v[r[i % n] as usize]; [v[0], v[1]] };
    // positive area
    let mut a: i128 = 0;
    for i in 0..n { let (x, y) = (p(i), p(i + 1)); a += x[0] as i128 * y[1] as i128 - y[0] as i128 * x[1] as i128; }
    if a <= 0 { return false; }
    for i in 0..n {
        for j in (i + 2)..n {
            if i == 0 && j == n - 1 { continue; }
            if iseg_cross(p(i), p(i + 1), p(j), p(j + 1)) {
                // shared endpoints of adjacent edges are fine; a repeated vertex is not
                return false;
            }
        }
    }
    // no repeated positions
    let mut pts: Vec<[i64; 2]> = (0..n).map(p).collect();
    pts.sort();
    pts.windows(2).all(|w| w[0] != w[1])
}

/// Ear clipping of a simple CCW ring (integer predicates), then Lawson flips of the inner edges.
fn triangulate(m: &Mesh, r: &[u32]) -> Option<Vec<[u32; 3]>> {
    let pts: Vec<[i64; 2]> = r.iter().map(|&i| { let v = m.v[i as usize]; [v[0], v[1]] }).collect();
    let mut idx: Vec<usize> = (0..r.len()).collect();
    let mut tris: Vec<[usize; 3]> = Vec::new();
    let mut guard = 0;
    while idx.len() > 3 {
        guard += 1;
        if guard > 4 * r.len() * r.len() + 100 { return None; }
        let n = idx.len();
        let mut found = None;
        for strict in [true, false] {
            for k in 0..n {
                let (a, b, c) = (idx[(k + n - 1) % n], idx[k], idx[(k + 1) % n]);
                if orient(pts[a], pts[b], pts[c]) <= 0 { continue; }
                let mut blocked = false;
                for &q in &idx {
                    if q == a || q == b || q == c { continue; }
                    let (o1, o2, o3) = (orient(pts[a], pts[b], pts[q]), orient(pts[b], pts[c], pts[q]), orient(pts[c], pts[a], pts[q]));
                    let inside = if strict { o1 >= 0 && o2 >= 0 && o3 >= 0 } else { o1 > 0 && o2 > 0 && o3 > 0 };
                    if inside { blocked = true; break; }
                }
                if !blocked { found = Some(k); break; }
            }
            if found.is_some() { break; }
        }
        let k = found?;
        let n = idx.len();
        tris.push([idx[(k + n - 1) % n], idx[k], idx[(k + 1) % n]]);
        idx.remove(k);
    }
    if idx.len() == 3 && orient(pts[idx[0]], pts[idx[1]], pts[idx[2]]) > 0 { tris.push([idx[0], idx[1], idx[2]]); }
    legalise(&pts, &mut tris, r.len());
    Some(tris.into_iter().map(|t| [r[t[0]], r[t[1]], r[t[2]]]).collect())
}

/// v3 gore.rs legalise (Lawson flips, strictly-inside test, ring edges kept), bounded.
fn legalise(points: &[[i64; 2]], tris: &mut [[usize; 3]], nring: usize) {
    let is_ring = |a: usize, b: usize| (a + 1) % nring == b || (b + 1) % nring == a;
    for _iter in 0..(4 * tris.len() + 16) {
        let mut edges = std::collections::BTreeMap::new();
        let mut flip = None;
        'search: for (i, tri) in tris.iter().enumerate() {
            for k in 0..3 {
                let (a, b, c) = (tri[k], tri[(k + 1) % 3], tri[(k + 2) % 3]);
                if let Some(&(d, j)) = edges.get(&(b, a)) {
                    if is_ring(a, b) { edges.insert((a, b), (c, i)); continue; }
                    let (pa, pb, pc, pd) = (points[a], points[b], points[c], points[d]);
                    if orient(pc, pd, pb) > 0 && orient(pd, pc, pa) > 0 && incircle(pa, pb, pc, pd) > 0 {
                        flip = Some((i, [c, d, b], j, [d, c, a]));
                        break 'search;
                    }
                }
                edges.insert((a, b), (c, i));
            }
        }
        match flip { Some((i, a, j, b)) => { tris[i] = a; tris[j] = b; } None => break }
    }
}

#[allow(dead_code)]
fn hull(m: &Mesh, r: &[u32]) -> Vec<u32> {
    let mut v: Vec<(i64, i64, u32)> = r.iter().map(|&i| { let p = m.v[i as usize]; (p[0], p[1], i) }).collect();
    v.sort();
    v.dedup_by(|a, b| a.0 == b.0 && a.1 == b.1);
    if v.len() < 3 { return v.iter().map(|x| x.2).collect(); }
    let mut lo: Vec<(i64, i64, u32)> = Vec::new();
    for &p in &v { while lo.len() >= 2 && orient([lo[lo.len() - 2].0, lo[lo.len() - 2].1], [lo[lo.len() - 1].0, lo[lo.len() - 1].1], [p.0, p.1]) <= 0 { lo.pop(); } lo.push(p); }
    let mut up: Vec<(i64, i64, u32)> = Vec::new();
    for &p in v.iter().rev() { while up.len() >= 2 && orient([up[up.len() - 2].0, up[up.len() - 2].1], [up[up.len() - 1].0, up[up.len() - 1].1], [p.0, p.1]) <= 0 { up.pop(); } up.push(p); }
    lo.pop(); up.pop();
    lo.extend(up);
    lo.iter().map(|x| x.2).collect()
}
