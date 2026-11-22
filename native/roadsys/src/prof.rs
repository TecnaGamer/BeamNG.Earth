//! G3 PROFILE: one vertical profile per carriageway (stroke), over ground and structure together.
//!
//! Knots every ~5 m along each stroke. Tier A (a stroke with no structure, tunnel or crossing floor): its own solve.
//! Tier B (vertical components): strokes joined by crossing floors, deck-level junctions and fork noses, one joint
//! solve. Objective: ground knots follow the DEM (weight h), structure / tunnel knots only smoothness (weight 1e-4 h),
//! plus LAMBDA = Ls^4 smoothing of z'' (Ls from the design speed). Pins: a stroke end at a ground junction node is held
//! at the node's DEM height (every stroke meeting there reads the same value: joined by construction); a deck-level
//! junction couples its stroke ends; a fork branch's nose station couples to the trunk's. Hard limits as an active set
//! (penalties, monotone, at most ROUNDS rounds; fixed order): grade (class limit, or the DEM's own local grade + 10 %
//! where steeper), comfort |z''| <= 0.3 m/s^2 / v^2 at the design speed, the crossing floors (clearance + deck depth),
//! deck over ground by 1.5 m away from abutments, tunnels 8 m under ground away from portals.
//!
//! Solver: the envelope LDL^T and reverse Cuthill-McKee order of native/earthfast/src/structures/heights.rs (the
//! loops copied as they are: fixed order of every sum, IEEE + - * / only, no FMA, no HashMap), on a matrix assembled
//! from triplets sorted by (row, col) with stable order. Output rounded to 1 mm.
use crate::geom::*;
use crate::io::Dem;
use crate::net::{Net, SUP_GROUND, SUP_STRUCT, SUP_TUNNEL};

pub const H_M: f64 = 5.0;
const ROUNDS: usize = 12;
const W_PIN: f64 = 1e7;
const W_FLOOR: f64 = 1e6;
const W_GRADE: f64 = 1e5;
const W_COMFORT: f64 = 1e5;
const W_SIDE: f64 = 1e5;
const A_MAX: f64 = 0.3;
const DECK_OVER_GROUND: f64 = 1.5;
const TUNNEL_COVER: f64 = 8.0;
const END_FREE_M: f64 = 15.0;
const EXPAND: bool = true;

#[derive(Clone, Debug, Default)]
pub struct ProfStats {
    pub components: usize,
    pub tier_a: usize,
    pub tier_b: usize,
    pub knots: usize,
    pub biggest_component_knots: usize,
    pub biggest_component_km: f64,
    pub rounds_max: usize,
    pub factor_fail: usize,
    pub crossings: usize,
    pub floors_met: usize,
    pub floor_short_max_m: f64,
    pub grade_viol: usize,
    pub comfort_viol: usize,
    pub couple_max_mm: f64,
}

pub struct Profiles {
    pub h: Vec<f64>,
    pub z: Vec<Vec<f64>>,
    pub comp_of: Vec<usize>,
    pub comp_cpu: Vec<f64>,
    pub stats: ProfStats,
}

impl Profiles {
    pub fn at(&self, si: usize, s: f64) -> f64 {
        let z = &self.z[si];
        if z.is_empty() { return 0.0; }
        if z.len() == 1 { return z[0]; }
        let h = self.h[si];
        let f = s / h;
        let mx = (z.len() - 1) as f64;
        let f = clampf(f, 0.0, mx);
        let i = (f as usize).min(z.len() - 2);
        let u = f - i as f64;
        z[i] * (1.0 - u) + z[i + 1] * u
    }
}

/// A linear form: sum of (var, coef) terms.
#[derive(Clone)]
struct Lin { t: [(usize, f64); 4], n: usize }

impl Lin {
    fn val(&self, z: &[f64]) -> f64 { let mut v = 0.0; for k in 0..self.n { v += self.t[k].1 * z[self.t[k].0]; } v }
}

/// An inequality lo <= form (or form <= hi), with weight.
struct Ineq { f: Lin, bound: f64, ge: bool, w: f64, kind: u8 }

fn lin_at(base: usize, nk: usize, h: f64, s: f64, sign: f64, t: &mut Vec<(usize, f64)>) {
    let f = clampf(s / h, 0.0, (nk - 1) as f64);
    let i = (f as usize).min(nk.saturating_sub(2));
    let u = f - i as f64;
    if nk == 1 { t.push((base, sign)); return; }
    t.push((base + i, sign * (1.0 - u)));
    t.push((base + i + 1, sign * u));
}

fn mklin(t: &[(usize, f64)]) -> Lin {
    let mut l = Lin { t: [(0, 0.0); 4], n: t.len().min(4) };
    for k in 0..l.n { l.t[k] = t[k]; }
    l
}

/// heights.rs rcm (copied).
fn rcm(adj: &[Vec<usize>]) -> Vec<usize> {
    let n = adj.len();
    let mut seen = vec![false; n];
    let mut order = Vec::with_capacity(n);
    let mut by: Vec<usize> = (0..n).collect();
    by.sort_by_key(|&i| (adj[i].len(), i));
    for &s in &by {
        if seen[s] { continue; }
        seen[s] = true;
        let mut q = std::collections::VecDeque::new();
        q.push_back(s);
        while let Some(v) = q.pop_front() {
            order.push(v);
            let mut nb: Vec<usize> = adj[v].iter().copied().filter(|&w| !seen[w]).collect();
            nb.sort_by_key(|&w| (adj[w].len(), w));
            for w in nb { if !seen[w] { seen[w] = true; q.push_back(w); } }
        }
    }
    order.reverse();
    order
}

/// heights.rs ldl_solve's loops on envelope rows (rows[i] holds columns first[i]..=i).
fn ldl(n: usize, first: &[usize], rows: &mut [Vec<f64>], rhs: &[f64]) -> Option<Vec<f64>> {
    let mut d = vec![0.0f64; n];
    for i in 0..n {
        let fi = first[i];
        for j in fi..i {
            let fj = first[j];
            let k0 = fi.max(fj);
            let mut acc = rows[i][j - fi];
            for k in k0..j { acc -= rows[i][k - fi] * d[k] * rows[j][k - fj]; }
            rows[i][j - fi] = acc / d[j];
        }
        let mut acc = rows[i][i - fi];
        for k in fi..i { let l = rows[i][k - fi]; acc -= l * l * d[k]; }
        if !(acc > 0.0) || !acc.is_finite() { return None; }
        d[i] = acc;
    }
    let mut y = rhs.to_vec();
    for i in 0..n {
        let fi = first[i];
        let mut acc = y[i];
        for k in fi..i { acc -= rows[i][k - fi] * y[k]; }
        y[i] = acc;
    }
    for i in 0..n { y[i] /= d[i]; }
    for i in (0..n).rev() {
        let fi = first[i];
        let yi = y[i];
        for k in fi..i { y[k] -= rows[i][k - fi] * yi; }
    }
    if y.iter().all(|v| v.is_finite()) { Some(y) } else { None }
}

/// Solve (sum_terms) on n variables in factor order pos: trip (i, j, v) for every (i, j) and (j, i) term pair.
fn solve_sym(n: usize, pos: &[usize], trip: &[(usize, usize, f64)], rhs: &[f64]) -> Option<Vec<f64>> {
    let mut low: Vec<(usize, usize, f64)> = trip.iter().filter_map(|&(i, j, v)| {
        let (a, b) = (pos[i], pos[j]);
        if a >= b { Some((a, b, v)) } else { None }
    }).collect();
    low.sort_by(|x, y| (x.0, x.1).cmp(&(y.0, y.1)));
    let mut first: Vec<usize> = (0..n).collect();
    for &(r, c, _) in &low { if c < first[r] { first[r] = c; } }
    let mut rows: Vec<Vec<f64>> = (0..n).map(|i| vec![0.0; i - first[i] + 1]).collect();
    for &(r, c, v) in &low { rows[r][c - first[r]] += v; }
    let mut b = vec![0.0; n];
    for i in 0..n { b[pos[i]] = rhs[i]; }
    let y = ldl(n, &first, &mut rows, &b)?;
    Some((0..n).map(|i| y[pos[i]]).collect())
}

/// Add w (form - r)^2 to the normal equations.
fn add_sq(trip: &mut Vec<(usize, usize, f64)>, rhs: &mut [f64], f: &Lin, r: f64, w: f64) {
    for a in 0..f.n {
        let (ia, ca) = f.t[a];
        rhs[ia] += w * ca * r;
        for b in 0..f.n {
            let (ib, cb) = f.t[b];
            trip.push((ia, ib, w * ca * cb));
        }
    }
}

fn grade_limit(net: &Net, si: usize) -> f64 {
    let st = &net.strokes[si];
    let link = st.parts.iter().any(|p| net.segs[net.edges[p.edge].seg].link);
    if st.cls == 1 && !link { 0.05 } else if st.cls == 2 && !link { 0.06 } else if link { 0.07 } else { 0.10 }
}

/// Each stroke's knot stations and the branch strokes' coupling stations.
pub fn solve(net: &Net, dem: &Dem, only: Option<&[bool]>) -> Profiles {
    let ns = net.strokes.len();
    let mut h = vec![H_M; ns];
    let mut nk = vec![0usize; ns];
    for si in 0..ns {
        let len = net.strokes[si].len();
        let m = ((len / H_M).ceil() as usize).max(1) + 1;
        nk[si] = m;
        h[si] = if m > 1 { len / (m - 1) as f64 } else { len.max(1.0) };
    }
    // union-find of vertical components
    let mut par: Vec<usize> = (0..ns).collect();
    fn find(p: &mut Vec<usize>, x: usize) -> usize { let mut r = x; while p[r] != r { r = p[r]; } let mut y = x; while p[y] != r { let n = p[y]; p[y] = r; y = n; } r }
    let unite = |p: &mut Vec<usize>, a: usize, b: usize| { let (ra, rb) = (find(p, a), find(p, b)); if ra != rb { let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) }; p[hi] = lo; } };
    for c in &net.crossings { unite(&mut par, c.a, c.b); }
    // node ends
    let mut ends: Vec<Vec<(usize, bool)>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() {
        ends[st.n0].push((si, true));
        ends[st.n1].push((si, false));
    }
    let end_sup = |si: usize, start: bool| {
        let st = &net.strokes[si];
        st.support_at(if start { 0.0 } else { st.len() }).kind
    };
    let mut deck_nodes: Vec<usize> = Vec::new();
    for n in 0..net.nodes.len() {
        if ends[n].len() >= 2 && ends[n].iter().any(|&(si, a)| end_sup(si, a) == SUP_STRUCT) {
            deck_nodes.push(n);
            for k in 1..ends[n].len() { unite(&mut par, ends[n][0].0, ends[n][k].0); }
        }
    }
    // fork noses: branch station (the stroke's cut, set by the layout) <-> trunk station
    let mut noses: Vec<(usize, f64, usize, f64)> = Vec::new();
    for (si, st) in net.strokes.iter().enumerate() {
        if let Some(sn) = st.snap0 { noses.push((si, st.cut0, sn.trunk, sn.s)); unite(&mut par, si, sn.trunk); }
        if let Some(sn) = st.snap1 { noses.push((si, st.len() - st.cut1, sn.trunk, sn.s)); unite(&mut par, si, sn.trunk); }
    }
    // a structure's component reaches one stroke past each junction at its ends (coupled there, not pinned), so
    // the solve can lower the crossed road and lift the approaches beyond a nearby junction (design G3 Tier B)
    let mut near: Vec<bool> = vec![false; ns];
    for c in &net.crossings { near[c.a] = true; near[c.b] = true; }
    for (si, st) in net.strokes.iter().enumerate() { if st.sup.iter().any(|u| u.kind != SUP_GROUND) { near[si] = true; } }
    let mut joined_nodes: Vec<usize> = Vec::new();
    if EXPAND {
        for si in 0..ns {
            if !near[si] { continue; }
            let st = &net.strokes[si];
            for node in [st.n0, st.n1] {
                if ends[node].len() < 2 || ends[node].len() > 6 { continue; }
                if ends[node].iter().any(|&(o, _)| net.strokes[o].len() > 600.0) { continue; }
                joined_nodes.push(node);
                for &(o, _) in &ends[node] { unite(&mut par, si, o); }
            }
        }
        joined_nodes.sort();
        joined_nodes.dedup();
    }
    let comp: Vec<usize> = (0..ns).map(|i| find(&mut par, i)).collect();
    let mut members: std::collections::BTreeMap<usize, Vec<usize>> = std::collections::BTreeMap::new();
    for si in 0..ns { members.entry(comp[si]).or_default().push(si); }
    let mut z: Vec<Vec<f64>> = vec![Vec::new(); ns];
    let mut stats = ProfStats::default();
    let mut comp_cpu = vec![0.0; ns];
    let node_z: Vec<f64> = net.nodes.iter().map(|n| dem.at(n.p)).collect();
    for (&root, mem) in &members {
        if let Some(o) = only { if !mem.iter().any(|&si| o[si]) { continue; } }
        let t0 = crate::cpu_now();
        let tier_b = mem.len() > 1 || net.strokes[mem[0]].sup.iter().any(|u| u.kind != SUP_GROUND);
        if tier_b { stats.tier_b += 1; } else { stats.tier_a += 1; }
        stats.components += 1;
        let mut base = vec![0usize; ns];
        let mut n = 0;
        let mut km = 0.0;
        for &si in mem { base[si] = n; n += nk[si]; km += net.strokes[si].len() / 1000.0; }
        stats.knots += n;
        if n > stats.biggest_component_knots { stats.biggest_component_knots = n; stats.biggest_component_km = km; }
        // ground samples, weights, limits
        let mut g = vec![0.0; n];
        let mut wg = vec![0.0; n];
        let mut glim = vec![0.0; n];      // per knot pair (i, i+1)
        let mut clim = vec![0.0; n];      // per interior knot
        let mut side: Vec<Ineq> = Vec::new();
        let mut cons: Vec<Ineq> = Vec::new();
        let mut trip0: Vec<(usize, usize, f64)> = Vec::new();
        let mut rhs0 = vec![0.0; n];
        for &si in mem {
            let st = &net.strokes[si];
            let (b, m, hh) = (base[si], nk[si], h[si]);
            let gl = grade_limit(net, si);
            let v = (st.kmh / 3.6).max(5.0);
            let kappa = A_MAX / (v * v);
            let ls = clampf(0.4 * st.kmh, 15.0, 60.0);
            let lam = ls * ls * ls * ls;
            for i in 0..m {
                let s = (i as f64 * hh).min(st.len());
                let (p, _) = st.line.at(s);
                g[b + i] = dem.at(p);
                let sup = st.support_at(s);
                wg[b + i] = if sup.kind == SUP_GROUND { hh } else { 1e-4 * hh };
                let far = s > sup.s0 + END_FREE_M && s < sup.s1 - END_FREE_M;
                if sup.kind == SUP_STRUCT && far {
                    side.push(Ineq { f: mklin(&[(b + i, 1.0)]), bound: g[b + i] + DECK_OVER_GROUND, ge: true, w: W_SIDE, kind: 3 });
                }
                if sup.kind == SUP_TUNNEL && far {
                    side.push(Ineq { f: mklin(&[(b + i, 1.0)]), bound: g[b + i] - TUNNEL_COVER, ge: false, w: W_SIDE, kind: 3 });
                }
            }
            for i in 0..m.saturating_sub(1) {
                // the DEM's own grade over +-4 knots where steeper than the class limit (+10 %)
                let (i0, i1) = (i.saturating_sub(4), (i + 5).min(m - 1));
                let dg = absf(g[b + i1] - g[b + i0]) / (((i1 - i0) as f64) * hh).max(1e-6);
                let sup = st.support_at(i as f64 * hh);
                glim[b + i] = if sup.kind == SUP_GROUND && dg * 1.1 > gl { dg * 1.1 } else { gl };
            }
            for i in 1..m.saturating_sub(1) { clim[b + i] = kappa * hh * hh; }
            // objective
            for i in 0..m {
                trip0.push((b + i, b + i, wg[b + i]));
                rhs0[b + i] += wg[b + i] * g[b + i];
            }
            let c = lam / (hh * hh * hh);
            for i in 1..m.saturating_sub(1) {
                let f = mklin(&[(b + i - 1, 1.0), (b + i, -2.0), (b + i + 1, 1.0)]);
                add_sq(&mut trip0, &mut rhs0, &f, 0.0, c);
            }
            // pins at ground junction nodes (a dead end and a deck node are free)
            for (start, node) in [(true, st.n0), (false, st.n1)] {
                let fork_branch = (start && st.snap0.is_some()) || (!start && st.snap1.is_some());
                if fork_branch || ends[node].len() < 2 { continue; }
                if net.fork_at[node].is_some() { continue; }
                if deck_nodes.binary_search(&node).is_ok() { continue; }
                if joined_nodes.binary_search(&node).is_ok() && ends[node].iter().all(|&(o, _)| comp[o] == root) {
                    // coupled ends: their common height is held near the node's DEM height (soft), not pinned
                    let k = if start { b } else { b + m - 1 };
                    add_sq(&mut trip0, &mut rhs0, &mklin(&[(k, 1.0)]), node_z[node], 10.0 * hh);
                    continue;
                }
                let k = if start { b } else { b + m - 1 };
                add_sq(&mut trip0, &mut rhs0, &mklin(&[(k, 1.0)]), node_z[node], W_PIN);
            }
            for i in 0..m.saturating_sub(1) {
                let f = mklin(&[(b + i + 1, 1.0), (b + i, -1.0)]);
                cons.push(Ineq { f: f.clone(), bound: glim[b + i] * hh, ge: false, w: W_GRADE, kind: 1 });
                cons.push(Ineq { f, bound: -glim[b + i] * hh, ge: true, w: W_GRADE, kind: 1 });
            }
            for i in 1..m.saturating_sub(1) {
                let f = mklin(&[(b + i - 1, 1.0), (b + i, -2.0), (b + i + 1, 1.0)]);
                cons.push(Ineq { f: f.clone(), bound: clim[b + i], ge: false, w: W_COMFORT, kind: 2 });
                cons.push(Ineq { f, bound: -clim[b + i], ge: true, w: W_COMFORT, kind: 2 });
            }
        }
        // couplings: deck-level nodes, fork noses
        let mut couples: Vec<Lin> = Vec::new();
        let mut cnodes: Vec<usize> = deck_nodes.clone();
        cnodes.extend(joined_nodes.iter().copied().filter(|&nd| ends[nd].iter().all(|&(o, _)| comp[o] == root)));
        cnodes.sort(); cnodes.dedup();
        for &nd in &cnodes {
            let e: Vec<(usize, bool)> = ends[nd].iter().copied().filter(|&(si, _)| comp[si] == root).collect();
            for k in 1..e.len() {
                let ka = if e[0].1 { base[e[0].0] } else { base[e[0].0] + nk[e[0].0] - 1 };
                let kb = if e[k].1 { base[e[k].0] } else { base[e[k].0] + nk[e[k].0] - 1 };
                couples.push(mklin(&[(ka, 1.0), (kb, -1.0)]));
            }
        }
        for &(bs, sb, ts, stn) in &noses {
            if comp[bs] != root { continue; }
            let mut t = Vec::new();
            lin_at(base[bs], nk[bs], h[bs], sb, 1.0, &mut t);
            lin_at(base[ts], nk[ts], h[ts], stn, -1.0, &mut t);
            couples.push(mklin(&t));
        }
        for f in &couples { add_sq(&mut trip0, &mut rhs0, f, 0.0, W_PIN); }
        // crossing floors
        let mut floors: Vec<usize> = Vec::new();
        for c in &net.crossings {
            if comp[c.a] != root { continue; }
            let (up, us, lo, ls_) = if c.upper == c.a { (c.a, c.sa, c.b, c.sb) } else { (c.b, c.sb, c.a, c.sa) };
            let mut t = Vec::new();
            lin_at(base[up], nk[up], h[up], us, 1.0, &mut t);
            lin_at(base[lo], nk[lo], h[lo], ls_, -1.0, &mut t);
            floors.push(cons.len());
            cons.push(Ineq { f: mklin(&t), bound: c.floor, ge: true, w: W_FLOOR, kind: 4 });
        }
        cons.extend(side);
        // factor order from the full pattern
        let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
        for &(i, j, _) in &trip0 { if i != j { adj[i].push(j); } }
        for c in &cons { for a in 0..c.f.n { for b2 in 0..c.f.n { if a != b2 { adj[c.f.t[a].0].push(c.f.t[b2].0); } } } }
        for a in adj.iter_mut() { a.sort(); a.dedup(); }
        let order = rcm(&adj);
        let mut pos = vec![0usize; n];
        for (k, &v) in order.iter().enumerate() { pos[v] = k; }
        let mut active = vec![false; cons.len()];
        let mut zz = g.clone();
        let mut rounds = 0;
        for _r in 0..ROUNDS {
            rounds += 1;
            let mut trip = trip0.clone();
            let mut rhs = rhs0.clone();
            for (k, c) in cons.iter().enumerate() { if active[k] { add_sq(&mut trip, &mut rhs, &c.f, c.bound, c.w); } }
            match solve_sym(n, &pos, &trip, &rhs) {
                Some(v) => zz = v,
                None => { stats.factor_fail += 1; break; }
            }
            let mut added = 0;
            for (k, c) in cons.iter().enumerate() {
                if active[k] { continue; }
                let v = c.f.val(&zz);
                let bad = if c.ge { v < c.bound - 1e-3 } else { v > c.bound + 1e-3 };
                if bad { active[k] = true; added += 1; }
            }
            if added == 0 { break; }
        }
        if rounds > stats.rounds_max { stats.rounds_max = rounds; }
        for v in zz.iter_mut() { *v = (*v * 1000.0).round() / 1000.0; }
        // checks
        for (k, c) in cons.iter().enumerate() {
            let v = c.f.val(&zz);
            let short = if c.ge { c.bound - v } else { v - c.bound };
            match c.kind {
                1 => if short > 0.005 * H_M { stats.grade_viol += 1; },
                2 => if short > 0.5 * absf(c.bound) + 0.002 { stats.comfort_viol += 1; },
                4 => {
                    let _ = k;
                    stats.crossings += 1;
                    if short <= 0.05 { stats.floors_met += 1; }
                    if short > stats.floor_short_max_m { stats.floor_short_max_m = short; }
                }
                _ => {}
            }
        }
        let _ = floors;
        for f in &couples { let e = absf(f.val(&zz)) * 1000.0; if e > stats.couple_max_mm { stats.couple_max_mm = e; } }
        for &si in mem { z[si] = zz[base[si]..base[si] + nk[si]].to_vec(); }
        let dt = crate::cpu_now() - t0;
        for &si in mem { comp_cpu[si] = dt; }
    }
    let _ = (SUP_TUNNEL,);
    Profiles { h, z, comp_of: comp, comp_cpu, stats }
}
