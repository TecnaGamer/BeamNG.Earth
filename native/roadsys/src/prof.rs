//! G3 PROFILE: one vertical profile per carriageway (stroke), over ground and structure together.
//!
//! Knots every ~5 m along each stroke.
//!
//! Tier A, every stroke on its own: ground knots follow the DEM (weight h), structure / tunnel knots only smoothness
//! (weight 1e-4 h), plus LAMBDA = Ls^4 smoothing of z'' (Ls from the design speed); its ends at a ground junction
//! node held at the node's DEM height (every stroke meeting there reads the same value: joined by construction); a
//! fork branch's or a cut joint's snapped end held at its trunk's Tier A height there (trunks first). Grade and
//! comfort limits as an active set. A pure function of the stroke, the DEM along it and its trunk's Tier A.
//!
//! Tier B, the approach rule (bridgerewrite/DESIGN.md G3; PT3 change 6): a section of a stroke reaches 2 x L_vc(V)
//! past each end of its structure or tunnel, each crossing floor, each deck-level junction and each fork nose or
//! joint inside such a section (L_vc = v^2 g / a_max: the vertical curve that takes the class's grade at the comfort
//! limit). Junction nodes inside a section give the other strokes there a secondary section of the same reach (no
//! further). Sections coupled by crossings, deck-level nodes, noses and joints form a vertical component, solved as
//! one QP; a section's inner ends are held to the Tier A profile (height and slope: two knots), so a component never
//! reaches past its approaches and no tile window ever cuts one. Components are capped at CAP_KM of sections (PT3
//! change 2): couplings are united in a fixed order, one that would pass the cap stays out (recorded), its floor
//! enforced on the upper road against the lower one's Tier A. (A grid bound on components was tried: bounded reads,
//! but one-sided floors at the squares' borders left 9 % of Newark's floors unmet against 1 %: not kept.)
//!
//! Hard limits as an active set (penalties, at most ROUNDS rounds; fixed order): grade (class limit, or the DEM's own
//! local grade + 10 % where steeper), comfort |z''| <= 0.3 m/s^2 / v^2 at the design speed, the crossing floors
//! (clearance + the structure's drawn depth), deck over ground by 1.5 m away from abutments, tunnels 8 m under ground
//! away from portals. A violated limit joins the set; one whose penalty's multiplier turns wrong-signed (the solution
//! held back on its feasible side) leaves it again, at most RELEASES times (M3: penalty rounds alone kept every limit
//! ever met as an equality). Limits still short after the rounds are counted (grade_viol, comfort_viol, floors short)
//! in each square's stats: where they are relaxed. Penalties, not an exact QP: a limit against the couplings or the
//! held section ends stays a little short and is recorded so.
//!
//! M3: a snapped branch's knots past its nose (inside the trunk's surface) lie at the trunk's height (Tier A pins,
//! Tier B couplings); see net.rs for the crossings no longer floored (decks side by side) and the floors' clearance.
//!
//! Solver: the envelope LDL^T and reverse Cuthill-McKee order of native/earthfast/src/structures/heights.rs (the
//! loops copied as they are: fixed order of every sum, IEEE + - * / only, no FMA, no HashMap), on a matrix assembled
//! from triplets sorted by (row, col) with stable order. Output rounded to 1 mm. Every solve is keyed by its whole
//! assembled problem (QP_FORMAT, the matrix, the right-hand side, every constraint): a solution is reused from the
//! process's store only for the very same problem (PT3 change 2's cache: a cold tile does not solve a big
//! component again).
use crate::geom::*;
use crate::net::{Net, SUP_GROUND, SUP_STRUCT, SUP_TUNNEL};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};

pub const H_M: f64 = 5.0;
pub const QP_FORMAT: u64 = 5;            // 2 (M2): split components, feasible floors, measured decks, the chord; 5 (M3): cut zones on the trunk, shallow and along crossings beside a deck
const ROUNDS: usize = 24;              // (M3: 12 -> 24, the active set's releases)
const RELEASES: u8 = 2;
const RELEASE_MULT: f64 = 1e-2;       // (M3) a multiplier past this, wrong-signed: the limit leaves the active set
const W_PIN: f64 = 1e7;
const W_COUPLE: f64 = 1e9;          // (M2) couplings over every other term: a nose, joint or deck node never parts
const W_FLOOR: f64 = 1e6;
const W_GRADE: f64 = 1e5;
const W_COMFORT: f64 = 1e5;
const W_SIDE: f64 = 1e5;
const A_MAX: f64 = 0.3;
const DECK_OVER_GROUND: f64 = 1.5;
const TUNNEL_COVER: f64 = 8.0;
const END_FREE_M: f64 = 15.0;
const DECK_DEPTH: f64 = crate::net::DECK_FULL_M;   // net.rs crossings' floor: clearance + this
// (a double deck's upper deck over the lower deck's clearance: today's builder draws it with its full depth too, M2;
// a stacked kit's thinner slab comes with M3)
const STACK_DEPTH: f64 = crate::net::DECK_FULL_M;
const FLOOR_SLACK: f64 = 0.05;
const MEASURED_APART_M: f64 = 2.0;
const MEASURED_NEAR_M: f64 = 60.0;  // (M3) m from a floor its decks' measurements are looked for  // (M3) m two measured decks may lie closer than a floor before it is left out
const DIG_MAX: f64 = 2.0;
const OBS_W: f64 = 1.0;
const W_CHORD: f64 = 0.05;          // a deck knot's pull toward its approaches' chord (per m: a ground knot's 1)
const CHORD_OUT_M: f64 = 10.0;
const UNDER_OBS_M: f64 = 25.0;
const UNDER_FILL_M: f64 = 4.0;      // m past a structure's half width its crossed road's ground is lined across
const UNDER_FILL_DZ: f64 = 0.3;     // m a ground point stands over that line to be taken for the structure      // m round a stroke's crossing under a structure its measurements are left out             // a measured deck knot's weight (per m: a ground knot's)           // m a ground road may dip under its ground (an underpass's cut)      // m a relaxed floor keeps under what the grades reach
pub const CAP_KM: f64 = 40.0;
const STORE_MAX: usize = 4096;

#[derive(Clone, Debug, Default)]
pub struct ProfStats {
    pub components: usize,
    pub tier_a: usize,
    pub tier_b: usize,
    pub sections: usize,
    pub secondary: usize,
    pub knots: usize,
    pub biggest_component_knots: usize,
    pub biggest_component_km: f64,
    pub cap_cuts: usize,
    pub rounds_max: usize,
    pub factor_fail: usize,
    pub crossings: usize,
    pub floors_met: usize,
    pub floor_short_max_m: f64,
    pub floors_relaxed: usize,          // floors the grade limits cannot reach from the sections' held ends
    pub floors_measured_out: usize,     // (M3) floors left out: both decks measured there, closer than the floor
    pub floor_relax_max_m: f64,
    pub stacked: usize,                 // floors of a double deck (the lower deck's clearance + STACK_DEPTH)
    pub grade_viol: usize,
    pub comfort_viol: usize,
    pub couple_max_mm: f64,
    pub solves: usize,
    pub reused: usize,
}

pub struct Profiles {
    pub h: Vec<f64>,
    pub z: Vec<Vec<f64>>,
    pub comp_of: Vec<usize>,          // stroke -> the component of its first section (usize::MAX: Tier A only)
    pub comp_cpu: Vec<f64>,
    pub keys: Vec<u64>,               // stroke -> the key of its profile (its Tier A, mixed with its components')
    pub sections: Vec<Vec<(f64, f64, usize)>>,   // stroke -> (s0, s1, component)
    pub stats: ProfStats,
    pub g: Vec<Vec<f64>>,             // (diagnostics) stroke -> the ground at its knots
    pub ob: Vec<Vec<f64>>,            // (diagnostics) stroke -> the measured deck heights at its knots (NaN: none)
    pub tier_a: Vec<Vec<f64>>,        // (diagnostics) stroke -> its Tier A profile
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
struct Ineq { f: Lin, bound: f64, ge: bool, w: f64, kind: u8, orig: f64 }

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

/// The cost of moving a stroke, by class (motorway 10, trunk 6, primary and ramps 2, the rest 1).
fn class_weight(net: &Net, si: usize) -> f64 {
    let st = &net.strokes[si];
    let link = st.parts.iter().any(|p| net.segs[net.edges[p.edge].seg].link);
    if link { 2.0 } else { match st.cls { 1 => 10.0, 2 => 6.0, 3 => 2.0, _ => 1.0 } }
}

/// The approach reach of a stroke: 2 x the vertical curve that takes its class's grade at the comfort limit.
pub fn reach_m(net: &Net, si: usize) -> f64 {
    let v = (net.strokes[si].kmh / 3.6).max(5.0);
    2.0 * clampf(v * v * grade_limit(net, si) / A_MAX, 30.0, 400.0)
}

// ------------------------------------------------------------------------------------------- the solve store

/// Solutions by the key of their whole problem (process-wide, bounded; a hit is the same problem's own answer).
fn store() -> &'static Mutex<(BTreeMap<u64, Arc<Vec<f64>>>, std::collections::VecDeque<u64>)> {
    static S: OnceLock<Mutex<(BTreeMap<u64, Arc<Vec<f64>>>, std::collections::VecDeque<u64>)>> = OnceLock::new();
    S.get_or_init(|| Mutex::new((BTreeMap::new(), std::collections::VecDeque::new())))
}

pub fn store_clear() {
    let mut s = store().lock().unwrap();
    s.0.clear();
    s.1.clear();
}

struct Hasher(u64);
impl Hasher {
    fn new() -> Hasher { Hasher(0xcbf29ce484222325 ^ QP_FORMAT.wrapping_mul(0x9e3779b97f4a7c15)) }
    fn u64(&mut self, v: u64) { for b in v.to_le_bytes() { self.0 ^= b as u64; self.0 = self.0.wrapping_mul(0x100000001b3); } }
    fn f64(&mut self, v: f64) { self.u64(v.to_bits()); }
}

/// A problem: n variables, the objective's triplets and right-hand side, the inequalities, an initial guess.
struct Qp { n: usize, trip: Vec<(usize, usize, f64)>, rhs: Vec<f64>, cons: Vec<Ineq>, guess: Vec<f64> }

impl Qp {
    fn key(&self) -> u64 {
        let mut h = Hasher::new();
        h.u64(self.n as u64);
        for &(i, j, v) in &self.trip { h.u64(i as u64); h.u64(j as u64); h.f64(v); }
        for &v in &self.rhs { h.f64(v); }
        for c in &self.cons {
            h.u64(c.f.n as u64);
            for k in 0..c.f.n { h.u64(c.f.t[k].0 as u64); h.f64(c.f.t[k].1); }
            h.f64(c.bound); h.u64(c.ge as u64); h.f64(c.w); h.u64(c.kind as u64);
        }
        for &v in &self.guess { h.f64(v); }
        h.0
    }

    /// The active-set solve (or the store's answer for this very problem), rounded to 1 mm.
    fn solve(&self, stats: &mut ProfStats) -> (Vec<f64>, u64) {
        let key = self.key();
        if let Some(v) = store().lock().unwrap().0.get(&key).cloned() {
            stats.reused += 1;
            return ((*v).clone(), key);
        }
        stats.solves += 1;
        let n = self.n;
        let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
        for &(i, j, _) in &self.trip { if i != j { adj[i].push(j); } }
        for c in &self.cons { for a in 0..c.f.n { for b2 in 0..c.f.n { if a != b2 { adj[c.f.t[a].0].push(c.f.t[b2].0); } } } }
        for a in adj.iter_mut() { a.sort(); a.dedup(); }
        let order = rcm(&adj);
        let mut pos = vec![0usize; n];
        for (k, &v) in order.iter().enumerate() { pos[v] = k; }
        let mut active = vec![false; self.cons.len()];
        let mut released = vec![0u8; self.cons.len()];
        let mut zz = self.guess.clone();
        let mut rounds = 0;
        for _r in 0..ROUNDS {
            rounds += 1;
            let mut trip = self.trip.clone();
            let mut rhs = self.rhs.clone();
            for (k, c) in self.cons.iter().enumerate() { if active[k] { add_sq(&mut trip, &mut rhs, &c.f, c.bound, c.w); } }
            match solve_sym(n, &pos, &trip, &rhs) {
                Some(v) => zz = v,
                None => { stats.factor_fail += 1; break; }
            }
            // (M3) an exact active set: a violated limit joins it; one in it that the solution leaves on its feasible
            // side (its penalty pulls toward the bound: a wrong-signed multiplier) leaves it, at most RELEASES times (no
            // cycling). Penalty rounds alone kept every limit ever met as an equality: curves held at the comfort bound
            // where nothing asked for them.
            let mut changed = 0;
            for (k, c) in self.cons.iter().enumerate() {
                let v = c.f.val(&zz);
                if active[k] {
                    // (its multiplier as the penalty gives it, 2 w (bound - value) for a lower bound: negative, the
                    // penalty holds the solution back from where it would go: the limit does not bind)
                    let slack = if c.ge { v - c.bound } else { c.bound - v };
                    if 2.0 * c.w * slack > RELEASE_MULT && released[k] < RELEASES { active[k] = false; released[k] += 1; changed += 1; }
                    continue;
                }
                let bad = if c.ge { v < c.bound - 1e-3 } else { v > c.bound + 1e-3 };
                if bad { active[k] = true; changed += 1; }
            }
            if changed == 0 { break; }
        }
        if rounds > stats.rounds_max { stats.rounds_max = rounds; }
        for v in zz.iter_mut() { *v = (*v * 1000.0).round() / 1000.0; }
        let mut s = store().lock().unwrap();
        if !s.0.contains_key(&key) {
            s.0.insert(key, Arc::new(zz.clone()));
            s.1.push_back(key);
            while s.1.len() > STORE_MAX { if let Some(k) = s.1.pop_front() { s.0.remove(&k); } }
        }
        (zz, key)
    }

    fn check(&self, z: &[f64], stats: &mut ProfStats) {
        for c in &self.cons {
            let v = c.f.val(z);
            let short = if c.ge { c.bound - v } else { v - c.bound };
            match c.kind {
                1 => if short > 0.005 * H_M { stats.grade_viol += 1; },
                2 => if short > 0.5 * absf(c.bound) + 0.002 { stats.comfort_viol += 1; },
                4 => {
                    // (against the true floor: a relaxed one is counted apart, floors_relaxed)
                    let short = if c.orig.is_nan() { short } else { c.orig - v };
                    stats.crossings += 1;
                    if short <= 0.05 { stats.floors_met += 1; }
                    if short > stats.floor_short_max_m { stats.floor_short_max_m = short; }
                }
                _ => {}
            }
        }
    }
}

/// One stroke's knot range [i0, i1] in a problem, its variables from `base`.
#[derive(Clone, Copy)]
struct Span { si: usize, i0: usize, i1: usize, base: usize }

/// The ground's chord under a structure interval at station s: linear between the ground at its two ends.
fn chord(st: &crate::net::Stroke, sup: crate::net::Sup, h: f64, gs: &[f64], s: f64) -> f64 {
    let m = gs.len();
    let at = |x: f64| -> f64 { let f = clampf(x / h, 0.0, (m - 1) as f64); let i = (f as usize).min(m.saturating_sub(2)); let u = f - i as f64;
                               if m < 2 { gs[0] } else { gs[i] * (1.0 - u) + gs[i + 1] * u } };
    let (a, b) = (sup.s0.max(0.0), sup.s1.min(st.len()));
    // (anchored on the approach ground CHORD_OUT_M outside each end: the DEM at a structure end is often the ground
    // below already, where the map's range was lengthened over a road under it)
    let (za, zb) = (at((a - CHORD_OUT_M).max(0.0)), at((b + CHORD_OUT_M).min(st.len())));
    if b - a <= 1e-6 { return za; }
    za + (zb - za) * clampf((s - a) / (b - a), 0.0, 1.0)
}

/// The objective and limits of stroke spans: DEM following, smoothing, grade, comfort, deck over ground, tunnel cover.
fn add_spans(net: &Net, spans: &[Span], h: &[f64], g: &[Vec<f64>], ob: &[Vec<f64>], qp: &mut Qp) {
    for sp in spans {
        let st = &net.strokes[sp.si];
        let (hh, gs) = (h[sp.si], &g[sp.si]);
        let gl = grade_limit(net, sp.si);
        let v = (st.kmh / 3.6).max(5.0);
        let kappa = A_MAX / (v * v);
        let ls = clampf(0.4 * st.kmh, 15.0, 60.0);
        let lam = ls * ls * ls * ls;
        let m = gs.len();
        let var = |i: usize| sp.base + (i - sp.i0);
        // (M2: the whole stroke's objective by its class, so its own shape (Tier A) is the same and moving it within a
        // component costs this much more: freeways stay down and the cross street rises, design G3)
        let cw = class_weight(net, sp.si);
        for i in sp.i0..=sp.i1 {
            let s = (i as f64 * hh).min(st.len());
            let sup = st.support_at(s);
            let w = cw * if sup.kind == SUP_GROUND { hh } else { 1e-4 * hh };
            qp.trip.push((var(i), var(i), w));
            qp.rhs[var(i)] += w * gs[i];
            // (M2: a deck knot pulled lightly toward the chord between its approaches' grounds: on a long deck with
            // nothing measured the curvature alone carried the approach ramps on into a 4-5 m hump, Seal Beach)
            if sup.kind == SUP_STRUCT {
                let c = chord(st, sup, hh, gs, s);
                qp.trip.push((var(i), var(i), cw * W_CHORD * hh));
                qp.rhs[var(i)] += cw * W_CHORD * hh * c;
            }
            // (M2: a measured deck height, a soft target with a ground knot's weight)
            if sup.kind == SUP_STRUCT {
                if let Some(&o) = ob[sp.si].get(i) { if o.is_finite() { qp.trip.push((var(i), var(i), cw * OBS_W * hh)); qp.rhs[var(i)] += cw * OBS_W * hh * o; } }
            }
            // (M2: a ground road dips at most DIG_MAX under its ground: a floor out of reach is relaxed, not dug)
            if sup.kind == SUP_GROUND {
                qp.cons.push(Ineq { f: mklin(&[(var(i), 1.0)]), bound: gs[i] - DIG_MAX, ge: true, w: W_SIDE, kind: 3, orig: f64::NAN });
            }
            let far = s > sup.s0 + END_FREE_M && s < sup.s1 - END_FREE_M;
            // (M2: only where the ground lies under the chord between the structure's two end grounds by more than
            // the clearance: a surface model holding the bridge itself, Copernicus' DSM, lifted every deck over it)
            if sup.kind == SUP_STRUCT && far && gs[i] + DECK_OVER_GROUND < chord(st, sup, hh, gs, s) {
                qp.cons.push(Ineq { f: mklin(&[(var(i), 1.0)]), bound: gs[i] + DECK_OVER_GROUND, ge: true, w: W_SIDE, kind: 3, orig: f64::NAN });
            }
            if sup.kind == SUP_TUNNEL && far {
                qp.cons.push(Ineq { f: mklin(&[(var(i), 1.0)]), bound: gs[i] - TUNNEL_COVER, ge: false, w: W_SIDE, kind: 3, orig: f64::NAN });
            }
        }
        let c = cw * lam / (hh * hh * hh);
        for i in (sp.i0 + 1)..sp.i1 {
            let f = mklin(&[(var(i - 1), 1.0), (var(i), -2.0), (var(i + 1), 1.0)]);
            add_sq(&mut qp.trip, &mut qp.rhs, &f, 0.0, c);
        }
        let measured = |i: usize| -> bool { ob[sp.si].get(i).map(|v| v.is_finite()).unwrap_or(false) };
        for i in sp.i0..sp.i1 {
            if measured(i) && measured(i + 1) { continue; }     // (M2: a measured deck's own grade)
            // the DEM's own grade over +-4 knots where steeper than the class limit (+10 %)
            let (a, b) = (i.saturating_sub(4), (i + 5).min(m - 1));
            let dg = absf(gs[b] - gs[a]) / (((b - a) as f64) * hh).max(1e-6);
            let sup = st.support_at(i as f64 * hh);
            let lim = if sup.kind == SUP_GROUND && dg * 1.1 > gl { dg * 1.1 } else { gl };
            let f = mklin(&[(var(i + 1), 1.0), (var(i), -1.0)]);
            qp.cons.push(Ineq { f: f.clone(), bound: lim * hh, ge: false, w: W_GRADE, kind: 1, orig: f64::NAN });
            qp.cons.push(Ineq { f, bound: -lim * hh, ge: true, w: W_GRADE, kind: 1, orig: f64::NAN });
        }
        for i in (sp.i0 + 1)..sp.i1 {
            if measured(i - 1) && measured(i) && measured(i + 1) { continue; }   // (M2: a measured deck's own curves)
            let f = mklin(&[(var(i - 1), 1.0), (var(i), -2.0), (var(i + 1), 1.0)]);
            let b = kappa * hh * hh;
            qp.cons.push(Ineq { f: f.clone(), bound: b, ge: false, w: W_COMFORT, kind: 2, orig: f64::NAN });
            qp.cons.push(Ineq { f, bound: -b, ge: true, w: W_COMFORT, kind: 2, orig: f64::NAN });
        }
    }
}

/// Union-find with the smaller root kept.
fn find(p: &mut [usize], x: usize) -> usize { let mut r = x; while p[r] != r { r = p[r]; } let mut y = x; while p[y] != r { let n = p[y]; p[y] = r; y = n; } r }

/// Every stroke's knot points (for the ground heights: g_of gets them in stroke order, then the nodes' points).
pub fn knot_points(net: &Net, need: Option<&[bool]>) -> (Vec<f64>, Vec<usize>, Vec<P2>) {
    let ns = net.strokes.len();
    let mut h = vec![H_M; ns];
    let mut nk = vec![0usize; ns];
    let mut pts = Vec::new();
    for si in 0..ns {
        let len = net.strokes[si].len();
        let m = ((len / H_M).ceil() as usize).max(1) + 1;
        nk[si] = m;
        h[si] = if m > 1 { len / (m - 1) as f64 } else { len.max(1.0) };
        if need.map(|v| v[si]).unwrap_or(true) {
            for i in 0..m { pts.push(net.strokes[si].line.at((i as f64 * h[si]).min(len)).0); }
        }
    }
    for n in &net.nodes { pts.push(n.p); }
    (h, nk, pts)
}

/// The profiles of the strokes `need` marks (all when None; Profiles of the others stay empty), ground heights from
/// g_of (the points of knot_points, in its order).
/// A Tier B section: knots [i0, i1] of stroke si (stations s0..s1); secondary: a junction neighbour's.
pub struct Sec { pub si: usize, pub i0: usize, pub i1: usize, pub s0: f64, pub s1: f64, pub secondary: bool }

/// A coupling of two sections: 0 a crossing floor (net.crossings[idx]), 1 a node (nodes[idx]), 2 a nose or joint
/// (stroke idx / 2, at its start when idx is odd); key: its fixed order.
#[derive(Clone)]
pub struct Cpl { pub key: (u8, u64, u64, i64, i64), pub a: usize, pub b: usize, pub what: u8, pub idx: usize }

/// The sections, couplings and components of the strokes `need` marks (topology and geometry only: no heights).
pub struct Plan {
    pub h: Vec<f64>,
    pub nk: Vec<usize>,
    pub secs: Vec<Sec>,
    pub sec_of: Vec<Vec<usize>>,
    pub cpl: Vec<Cpl>,
    pub joined: Vec<bool>,
    pub root: Vec<usize>,
    pub full_root: Vec<usize>,      // section -> its component before any split (reads take all of it)
    pub ends: Vec<Vec<(usize, bool)>>,
    pub deck_node: Vec<bool>,
    pub cap_cuts: usize,
}

impl Plan {
    pub fn sec_at(&self, si: usize, s: f64) -> Option<usize> {
        self.sec_of[si].iter().copied().find(|&k| s >= self.secs[k].s0 - 1e-6 && s <= self.secs[k].s1 + 1e-6)
    }
}

/// The split of every full component over CAP_KM (bridgerewrite/DESIGN.md G3, M2): a data-only rule on the component
/// itself. Unbreakable couplings (noses, joints, deck-level nodes) are contracted first; a part over the cap is ordered
/// breadth-first from its smallest section along its couplings (neighbours in index order) and cut where the fewest
/// breakable couplings cross between the prefix and the rest, each side at least a fifth of the part (else a
/// twentieth; ties: the more even split, then the shorter prefix); the cut couplings stay out (a cut floor is held
/// against the lower road's Tier A, a cut node at the node's own ground). Again on every resulting part until all fit
/// or a part is one unbreakable group (kept whole, recorded). Returns (joined per coupling, couplings cut).
fn split_components(secs: &[Sec], cpl: &[Cpl], full_root: &[usize], unbreakable: &[bool]) -> (Vec<bool>, usize) {
    let nsec = secs.len();
    let mut joined = vec![true; cpl.len()];
    let mut cuts = 0usize;
    let km: Vec<f64> = secs.iter().map(|x| (x.s1 - x.s0) / 1000.0).collect();
    let mut groups: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for i in 0..nsec { groups.entry(full_root[i]).or_default().push(i); }
    let mut of_group: BTreeMap<usize, Vec<usize>> = BTreeMap::new();     // root -> couplings
    for (k, c) in cpl.iter().enumerate() { of_group.entry(full_root[c.a]).or_default().push(k); }
    for (root, mem) in &groups {
        let total: f64 = mem.iter().map(|&i| km[i]).sum();
        if total <= CAP_KM { continue; }
        let ks = of_group.get(root).cloned().unwrap_or_default();
        // super-nodes: sections joined by unbreakable couplings
        let mut sp: Vec<usize> = (0..nsec).collect();
        for &k in &ks {
            if !unbreakable[k] { continue; }
            let (ra, rb) = (find(&mut sp, cpl[k].a), find(&mut sp, cpl[k].b));
            if ra != rb { let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) }; sp[hi] = lo; }
        }
        let sup_of: BTreeMap<usize, usize> = mem.iter().map(|&i| (i, find(&mut sp, i))).collect();
        let mut sup_km: BTreeMap<usize, f64> = BTreeMap::new();
        for &i in mem { *sup_km.entry(sup_of[&i]).or_insert(0.0) += km[i]; }
        let mut work: Vec<Vec<usize>> = vec![sup_km.keys().copied().collect()];
        while let Some(part) = work.pop() {
            let pkm: f64 = part.iter().map(|v| sup_km[v]).sum();
            if pkm <= CAP_KM || part.len() < 2 { continue; }
            let inpart: std::collections::BTreeSet<usize> = part.iter().copied().collect();
            // breakable couplings still joined inside the part, as super-node edges
            let mut adj: BTreeMap<usize, Vec<(usize, usize)>> = BTreeMap::new();
            for &k in &ks {
                if unbreakable[k] || !joined[k] { continue; }
                let (a, b) = (sup_of[&cpl[k].a], sup_of[&cpl[k].b]);
                if a == b || !inpart.contains(&a) || !inpart.contains(&b) { continue; }
                adj.entry(a).or_default().push((b, k));
                adj.entry(b).or_default().push((a, k));
            }
            for v in adj.values_mut() { v.sort(); }
            // breadth-first order from the smallest super-node (then the smallest unvisited)
            let mut order: Vec<usize> = Vec::with_capacity(part.len());
            let mut seen: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
            for &s0 in &part {
                if seen.contains(&s0) { continue; }
                seen.insert(s0);
                let mut q = std::collections::VecDeque::new();
                q.push_back(s0);
                while let Some(v) = q.pop_front() {
                    order.push(v);
                    if let Some(nb) = adj.get(&v) { for &(w, _) in nb { if seen.insert(w) { q.push_back(w); } } }
                }
            }
            let pos: BTreeMap<usize, usize> = order.iter().enumerate().map(|(i, &v)| (v, i)).collect();
            // sweep: cut count between order[..t] and the rest
            let mut best: Option<(usize, i64, usize)> = None;      // (cut count, imbalance mm, t)
            for pass in 0..2 {
                let min_side = if pass == 0 { 0.2 } else { 0.05 } * pkm;
                let mut cut = 0i64;
                let mut acc = 0.0;
                for t in 1..order.len() {
                    let v = order[t - 1];
                    acc += sup_km[&v];
                    if let Some(nb) = adj.get(&v) { for &(w, _) in nb { if pos[&w] < t - 1 { cut -= 1; } else { cut += 1; } } }
                    if acc < min_side || pkm - acc < min_side { continue; }
                    let cand = (cut.max(0) as usize, ((2.0 * acc - pkm).abs() * 1e6).round() as i64, t);
                    if best.map(|b| cand < b).unwrap_or(true) { best = Some(cand); }
                }
                if best.is_some() { break; }
            }
            let Some((_, _, t)) = best else { continue };
            for (v, nb) in &adj {
                if pos[v] >= t { continue; }
                for &(w, k) in nb { if pos[&w] >= t && joined[k] { joined[k] = false; cuts += 1; } }
            }
            // the parts left: connected through what stays joined
            let mut pp: BTreeMap<usize, usize> = part.iter().map(|&v| (v, v)).collect();
            fn f2(p: &mut BTreeMap<usize, usize>, x: usize) -> usize { let mut r = x; while p[&r] != r { r = p[&r]; } r }
            for (v, nb) in &adj {
                for &(w, k) in nb {
                    if !joined[k] { continue; }
                    let (ra, rb) = (f2(&mut pp, *v), f2(&mut pp, w));
                    if ra != rb { let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) }; pp.insert(hi, lo); }
                }
            }
            let mut sub: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
            for &v in &part { let r = f2(&mut pp, v); sub.entry(r).or_default().push(v); }
            if sub.len() < 2 { continue; }
            for (_r, p) in sub.into_iter().rev() { work.push(p); }
        }
    }
    (joined, cuts)
}

pub fn plan(net: &Net, need: Option<&[bool]>) -> Plan { plan_w(net, need, None) }

/// plan() with the sections cut to a window (heights' squares, square.rs): each section keeps its runs of knots inside
/// the box (min x, min y, max x, max y); a cut end is an inner end (held to Tier A, height and slope) and a coupling
/// outside the window holds nothing.
pub fn plan_w(net: &Net, need: Option<&[bool]>, window: Option<[f64; 4]>) -> Plan {
    let ns = net.strokes.len();
    let want = |si: usize| need.map(|v| v[si]).unwrap_or(true);
    let (h, nk, _pts) = knot_points(net, Some(&vec![false; ns]));
    let mut ends: Vec<Vec<(usize, bool)>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() {
        ends[st.n0].push((si, true));
        ends[st.n1].push((si, false));
    }
    let end_sup = |si: usize, start: bool| { let st = &net.strokes[si]; st.support_at(if start { 0.0 } else { st.len() }).kind };
    let deck_node: Vec<bool> = (0..net.nodes.len()).map(|n| ends[n].len() >= 2 && ends[n].iter().any(|&(si, a)| end_sup(si, a) == SUP_STRUCT)).collect();
    let snap_station = |si: usize, start: bool| -> f64 { let st = &net.strokes[si]; if start { st.cut0 } else { st.len() - st.cut1 } };
    let mut cap_cuts = 0usize;
    // ---- Tier B sections (the approach rule)
    let reach: Vec<f64> = (0..ns).map(|si| reach_m(net, si)).collect();
    let mut prim: Vec<Vec<(f64, f64)>> = vec![Vec::new(); ns];
    for (si, st) in net.strokes.iter().enumerate() {
        if !want(si) { continue; }
        for u in &st.sup { if u.kind != SUP_GROUND { prim[si].push((u.s0 - reach[si], u.s1 + reach[si])); } }
    }
    for c in &net.crossings {
        if want(c.a) { prim[c.a].push((c.sa - reach[c.a], c.sa + reach[c.a])); }
        if want(c.b) { prim[c.b].push((c.sb - reach[c.b], c.sb + reach[c.b])); }
    }
    for n in 0..net.nodes.len() {
        if !deck_node[n] { continue; }
        for &(si, a) in &ends[n] {
            if !want(si) { continue; }
            let len = net.strokes[si].len();
            prim[si].push(if a { (-1.0, reach[si]) } else { (len - reach[si], len + 1.0) });
        }
    }
    let merge = |v: &mut Vec<(f64, f64)>, len: f64| {
        for x in v.iter_mut() { x.0 = x.0.max(0.0); x.1 = x.1.min(len); }
        v.retain(|x| x.1 - x.0 > 1e-6);
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        let mut out: Vec<(f64, f64)> = Vec::new();
        for x in v.drain(..) {
            if let Some(l) = out.last_mut() { if x.0 <= l.1 + 2.0 * H_M { if x.1 > l.1 { l.1 = x.1; } continue; } }
            out.push(x);
        }
        *v = out;
    };
    let inside = |v: &[(f64, f64)], s: f64| v.iter().any(|x| s >= x.0 - 1e-6 && s <= x.1 + 1e-6);
    for si in 0..ns { let len = net.strokes[si].len(); merge(&mut prim[si], len); }
    // noses and joints inside a trunk's section reach into the snapped stroke (twice: chains of joints)
    for _ in 0..3 {
        let mut add: Vec<(usize, (f64, f64))> = Vec::new();
        for (si, st) in net.strokes.iter().enumerate() {
            if !want(si) { continue; }
            for (start, sn) in [(true, st.snap0), (false, st.snap1)] {
                let Some(sn) = sn else { continue };
                if !want(sn.trunk) { continue; }
                let s = snap_station(si, start);
                if inside(&prim[sn.trunk], sn.s) && !inside(&prim[si], s) {
                    // (from the stroke's own end: the branch has no inner end at its nose, nothing pins it there)
                    add.push((si, if start { (-1.0, s + reach[si]) } else { (s - reach[si], net.strokes[si].len() + 1.0) }));
                }
                if inside(&prim[si], s) && !inside(&prim[sn.trunk], sn.s) {
                    add.push((sn.trunk, (sn.s - reach[sn.trunk], sn.s + reach[sn.trunk])));
                }
            }
        }
        if add.is_empty() { break; }
        for (si, x) in add { prim[si].push(x); }
        for si in 0..ns { let len = net.strokes[si].len(); merge(&mut prim[si], len); }
    }
    // secondary sections: a ground junction node at a primary section's end gives the node's other strokes one
    let mut sec: Vec<Vec<(f64, f64)>> = vec![Vec::new(); ns];
    for n in 0..net.nodes.len() {
        if deck_node[n] || ends[n].len() < 2 { continue; }
        let touched = ends[n].iter().any(|&(si, a)| want(si) && inside(&prim[si], if a { 0.0 } else { net.strokes[si].len() }));
        if !touched { continue; }
        for &(si, a) in &ends[n] {
            if !want(si) { continue; }
            let len = net.strokes[si].len();
            let e = if a { 0.0 } else { len };
            if inside(&prim[si], e) { continue; }
            sec[si].push(if a { (-1.0, reach[si]) } else { (len - reach[si], len + 1.0) });
        }
    }
    // (M2) a nose or joint inside a trunk's secondary section reaches into the snapped stroke too, and one inside the
    // branch's reaches into its trunk: no nose left with one side solved and the other at Tier A (0.36 m, Newark)
    {
        let mut add: Vec<(usize, (f64, f64))> = Vec::new();
        for (si, st) in net.strokes.iter().enumerate() {
            if !want(si) { continue; }
            for (start, sn) in [(true, st.snap0), (false, st.snap1)] {
                let Some(sn) = sn else { continue };
                if !want(sn.trunk) { continue; }
                let s = snap_station(si, start);
                let tr = inside(&prim[sn.trunk], sn.s) || inside(&sec[sn.trunk], sn.s);
                let br = inside(&prim[si], s) || inside(&sec[si], s);
                if tr && !br {
                    add.push((si, if start { (-1.0, s + reach[si]) } else { (s - reach[si], net.strokes[si].len() + 1.0) }));
                }
                if br && !tr { add.push((sn.trunk, (sn.s - reach[sn.trunk], sn.s + reach[sn.trunk]))); }
            }
        }
        for (si, x) in add { sec[si].push(x); }
    }
    // sections as knot ranges; ids in stroke order
    let mut secs: Vec<Sec> = Vec::new();
    let mut sec_of: Vec<Vec<usize>> = vec![Vec::new(); ns];
    for si in 0..ns {
        if !want(si) { continue; }
        let len = net.strokes[si].len();
        let mut all: Vec<(f64, f64, bool)> = prim[si].iter().map(|x| (x.0, x.1, false)).collect();
        let mut sv = sec[si].clone();
        merge(&mut sv, len);
        for x in sv { if !all.iter().any(|y| x.0 < y.1 && x.1 > y.0) { all.push((x.0, x.1, true)); } }
        all.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        for (s0, s1, secondary) in all {
            let i0 = ((s0 / h[si]).floor() as usize).min(nk[si] - 1);
            let i1 = ((s1 / h[si]).ceil() as usize).min(nk[si] - 1);
            if i1 < i0 + 1 { continue; }      // (M2: a short arm too, two knots: every arm of a node can join)
            let mut runs: Vec<(usize, usize)> = Vec::new();
            match window {
                None => runs.push((i0, i1)),
                Some(w) => {
                    let inside = |i: usize| { let p = net.strokes[si].line.at((i as f64 * h[si]).min(len)).0; p[0] >= w[0] && p[0] <= w[2] && p[1] >= w[1] && p[1] <= w[3] };
                    let mut a: Option<usize> = None;
                    for i in i0..=i1 {
                        if inside(i) { if a.is_none() { a = Some(i); } }
                        else if let Some(x) = a.take() { if i - 1 >= x + 1 { runs.push((x, i - 1)); } }
                    }
                    if let Some(x) = a { if i1 >= x + 1 { runs.push((x, i1)); } }
                }
            }
            for (a, b) in runs {
                sec_of[si].push(secs.len());
                secs.push(Sec { si, i0: a, i1: b, s0: a as f64 * h[si], s1: (b as f64 * h[si]).min(len), secondary });
            }
        }
    }
    let sec_at = |si: usize, s: f64| -> Option<usize> {
        sec_of[si].iter().copied().find(|&k| s >= secs[k].s0 - 1e-6 && s <= secs[k].s1 + 1e-6)
    };
    // couplings, in a fixed order: (kind, the strokes' stable ids, the stations in mm)
    let mut cpl: Vec<Cpl> = Vec::new();
    for (ci, c) in net.crossings.iter().enumerate() {
        if let (Some(a), Some(b)) = (sec_at(c.a, c.sa), sec_at(c.b, c.sb)) {
            cpl.push(Cpl { key: (0, net.strokes[c.a].uid, net.strokes[c.b].uid, mm(c.sa), mm(c.sb)), a, b, what: 0, idx: ci });
        }
    }
    for n in 0..net.nodes.len() {
        if ends[n].len() < 2 { continue; }
        let e: Vec<(usize, bool)> = ends[n].iter().copied().filter(|&(si, a)| want(si) && sec_at(si, if a { 0.0 } else { net.strokes[si].len() }).is_some()).collect();
        // (M2: a node moves only with all its arms: one arm left out, every one there is held at the node's ground;
        // a node with some arms in a component and one at Tier A stepped up to 6 m, Newark)
        if e.len() < ends[n].len() { continue; }
        for k in 1..e.len() {
            let sa = sec_at(e[0].0, if e[0].1 { 0.0 } else { net.strokes[e[0].0].len() }).unwrap();
            let sb = sec_at(e[k].0, if e[k].1 { 0.0 } else { net.strokes[e[k].0].len() }).unwrap();
            cpl.push(Cpl { key: (1, net.strokes[e[0].0].uid, net.strokes[e[k].0].uid, e[0].1 as i64, e[k].1 as i64), a: sa, b: sb, what: 1, idx: n });
        }
    }
    for (si, st) in net.strokes.iter().enumerate() {
        if !want(si) { continue; }
        for (start, sn) in [(true, st.snap0), (false, st.snap1)] {
            let Some(sn) = sn else { continue };
            if !want(sn.trunk) { continue; }
            if let (Some(a), Some(b)) = (sec_at(si, snap_station(si, start)), sec_at(sn.trunk, sn.s)) {
                cpl.push(Cpl { key: (2, st.uid, net.strokes[sn.trunk].uid, start as i64, mm(sn.s)), a, b, what: 2, idx: si * 2 + start as usize });
            }
        }
    }
    cpl.sort_by(|x, y| x.key.cmp(&y.key));
    let nsec = secs.len();
    // full components: every coupling united (the split rule's input: a function of the component alone, never of
    // what else a read holds)
    let mut fpar: Vec<usize> = (0..nsec).collect();
    for c in &cpl {
        let (ra, rb) = (find(&mut fpar, c.a), find(&mut fpar, c.b));
        if ra != rb { let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) }; fpar[hi] = lo; }
    }
    let full_root: Vec<usize> = (0..nsec).map(|i| find(&mut fpar, i)).collect();
    // never cut: a nose or joint (the branch's row is the trunk's there) and a deck-level node (no common ground
    // height holds both sides)
    let unbreakable: Vec<bool> = cpl.iter().map(|c| c.what == 2 || (c.what == 1 && deck_node[c.idx])).collect();
    let (joined, cuts) = split_components(&secs, &cpl, &full_root, &unbreakable);
    cap_cuts += cuts;
    let mut par: Vec<usize> = (0..nsec).collect();
    for (k, c) in cpl.iter().enumerate() {
        if !joined[k] { continue; }
        let (ra, rb) = (find(&mut par, c.a), find(&mut par, c.b));
        if ra != rb { let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) }; par[hi] = lo; }
    }
    let root: Vec<usize> = (0..nsec).map(|i| find(&mut par, i)).collect();
    Plan { h, nk, secs, sec_of, cpl, joined, root, full_root, ends, deck_node, cap_cuts }
}

pub fn solve(net: &Net, g_of: &dyn Fn(&[P2]) -> Vec<f64>, need: Option<&[bool]>) -> Profiles { solve_w(net, g_of, need, None) }

/// solve() with the Tier B sections cut to a window (plan_w).
pub fn solve_w(net: &Net, g_of: &dyn Fn(&[P2]) -> Vec<f64>, need: Option<&[bool]>, window: Option<[f64; 4]>) -> Profiles {
    solve_obs(net, g_of, None, need, window)
}

/// A structure run of a stroke for the measurements: its knots' points, the road's half width, its level.
pub struct ObsRun { pub si: usize, pub i0: usize, pub pts: Vec<P2>, pub half: f64, pub level: f64 }

/// solve_w with measured deck heights (M2): obs_of(runs) gives per run the measured road surface height at each of
/// its knots (NaN: none; roadz's lidar, BD TOPO, NVDB as today's deck builder reads them), a soft target of a ground
/// knot's weight on those structure knots (OBS_W): the floors, grades and comfort stay as they are.
pub fn solve_obs(net: &Net, g_of: &dyn Fn(&[P2]) -> Vec<f64>, obs_of: Option<&dyn Fn(&[ObsRun]) -> Vec<Vec<f64>>>,
                 need: Option<&[bool]>, window: Option<[f64; 4]>) -> Profiles {
    let ns = net.strokes.len();
    let want = |si: usize| need.map(|v| v[si]).unwrap_or(true);
    let (h, nk, pts) = knot_points(net, need);
    let gz = g_of(&pts);
    let mut g: Vec<Vec<f64>> = vec![Vec::new(); ns];
    let mut k = 0;
    for si in 0..ns { if want(si) { g[si] = gz[k..k + nk[si]].to_vec(); k += nk[si]; } }
    // (M2) a ground road's ground under a structure crossing over it: where the DEM rises over the line across the
    // structure's width (a surface model holding the deck itself: Canada's HRDEM, Copernicus), that line (today's
    // grading takes such bridges out of the ground too: DECK_GROUND)
    let mut offs_u = Vec::new();
    let mut under_iv: BTreeMap<usize, Vec<(f64, f64)>> = BTreeMap::new();
    for c in &net.crossings {
        let (up, us, lo, ls) = if c.upper == c.a { (c.a, c.sa, c.b, c.sb) } else { (c.b, c.sb, c.a, c.sa) };
        if !want(lo) || g[lo].is_empty() || net.strokes[up].support_at(us).kind != SUP_STRUCT { continue; }
        if net.strokes[lo].support_at(ls).kind != SUP_GROUND { continue; }
        net.strokes[up].offsets(us, &mut offs_u);
        let hw = if offs_u.is_empty() { 5.0 } else { offs_u[0].max(-offs_u[offs_u.len() - 1]) };
        under_iv.entry(lo).or_default().push((ls - hw - UNDER_FILL_M, ls + hw + UNDER_FILL_M));
    }
    // (the crossed ranges of one road merged first: three ramps over one street, each range's ends under the next deck)
    for (lo, mut iv) in under_iv {
        iv.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        let mut merged: Vec<(f64, f64)> = Vec::new();
        for x in iv { if let Some(l) = merged.last_mut() { if x.0 <= l.1 { if x.1 > l.1 { l.1 = x.1; } continue; } } merged.push(x); }
        let m = g[lo].len();
        let hl = h[lo];
        for (a, b) in merged {
            let i0 = ((a / hl).floor().max(0.0) as usize).min(m - 1);
            let i1 = ((b / hl).ceil() as usize).min(m - 1);
            if i1 < i0 + 2 { continue; }
            let (za, zb) = (g[lo][i0], g[lo][i1]);
            for i in (i0 + 1)..i1 {
                let line = za + (zb - za) * ((i - i0) as f64 / (i1 - i0) as f64);
                if g[lo][i] > line + UNDER_FILL_DZ { g[lo][i] = line; }
            }
        }
    }
    let mut ob: Vec<Vec<f64>> = vec![Vec::new(); ns];
    if let Some(f) = obs_of {
        let mut runs: Vec<ObsRun> = Vec::new();
        let mut offs = Vec::new();
        // (not under another structure: a measurement there is the upper deck's top, as today's _covered_by_higher)
        let mut under: Vec<Vec<f64>> = vec![Vec::new(); ns];
        for c in &net.crossings {
            let lo = if c.upper == c.a { c.b } else { c.a };
            let ls = if c.upper == c.a { c.sb } else { c.sa };
            if net.strokes[c.upper].support_at(if c.upper == c.a { c.sa } else { c.sb }).kind == SUP_STRUCT
                && net.strokes[lo].support_at(ls).kind == SUP_STRUCT { under[lo].push(ls); }
        }
        for si in 0..ns {
            if !want(si) { continue; }
            let st = &net.strokes[si];
            for u in st.sup.iter().filter(|u| u.kind == SUP_STRUCT) {
                let i0 = (u.s0 / h[si]).ceil() as usize;
                let i1 = ((u.s1 / h[si]).floor() as usize).min(nk[si] - 1);
                if i1 < i0 + 1 { continue; }
                let mut r = ObsRun { si, i0, pts: Vec::new(), half: 0.0, level: u.level };
                for i in i0..=i1 { r.pts.push(st.line.at((i as f64 * h[si]).min(st.len())).0); }
                st.offsets(0.5 * (u.s0 + u.s1), &mut offs);
                r.half = if offs.is_empty() { 3.5 } else { offs[0].max(-offs[offs.len() - 1]) };
                runs.push(r);
            }
        }
        let got = f(&runs);
        for (r, z) in runs.iter().zip(got.iter()) {
            if z.len() != r.pts.len() { continue; }
            if ob[r.si].is_empty() { ob[r.si] = vec![f64::NAN; nk[r.si]]; }
            for (j, &v) in z.iter().enumerate() {
                let s = (r.i0 + j) as f64 * h[r.si];
                if v.is_finite() && !under[r.si].iter().any(|&x| absf(x - s) <= UNDER_OBS_M) { ob[r.si][r.i0 + j] = v; }
            }
        }
    }
    let node_z: Vec<f64> = gz[k..].to_vec();
    let mut stats = ProfStats::default();
    let mut ends: Vec<Vec<(usize, bool)>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() {
        ends[st.n0].push((si, true));
        ends[st.n1].push((si, false));
    }
    let end_sup = |si: usize, start: bool| { let st = &net.strokes[si]; st.support_at(if start { 0.0 } else { st.len() }).kind };
    let deck_node: Vec<bool> = (0..net.nodes.len()).map(|n| ends[n].len() >= 2 && ends[n].iter().any(|&(si, a)| end_sup(si, a) == SUP_STRUCT)).collect();
    let pin_node = |node: usize| ends[node].len() >= 2 && net.fork_at[node].is_none() && !deck_node[node];
    // ---- Tier A, trunks before the strokes snapped to them
    fn depth_of(net: &Net, si: usize, d: usize) -> usize {
        if d >= 8 { return d; }
        let st = &net.strokes[si];
        let mut best = d;
        for sn in [st.snap0, st.snap1].into_iter().flatten() { best = best.max(depth_of(net, sn.trunk, d + 1)); }
        best
    }
    let mut order: Vec<usize> = (0..ns).filter(|&si| want(si)).collect();
    order.sort_by_key(|&si| (depth_of(net, si, 0), net.strokes[si].uid, si));
    let mut z: Vec<Vec<f64>> = vec![Vec::new(); ns];
    let mut keys = vec![0u64; ns];
    let mut comp_cpu = vec![0.0; ns];
    let snap_station = |si: usize, start: bool| -> f64 { let st = &net.strokes[si]; if start { st.cut0 } else { st.len() - st.cut1 } };
    for &si in &order {
        let t0 = crate::cpu_now();
        let st = &net.strokes[si];
        let m = nk[si];
        let mut qp = Qp { n: m, trip: Vec::new(), rhs: vec![0.0; m], cons: Vec::new(), guess: g[si].clone() };
        add_spans(net, &[Span { si, i0: 0, i1: m - 1, base: 0 }], &h, &g, &ob, &mut qp);
        for (start, node) in [(true, st.n0), (false, st.n1)] {
            let kv = if start { 0 } else { m - 1 };
            let snap = if start { st.snap0 } else { st.snap1 };
            if let Some(sn) = snap {
                if !z[sn.trunk].is_empty() {
                    // held at the trunk's Tier A height at the nose / joint (one way: the trunk never reads this)
                    let zt = { let zt = &z[sn.trunk]; at_knots(zt, h[sn.trunk], sn.s) };
                    let mut t = Vec::new();
                    lin_at(0, m, h[si], snap_station(si, start), 1.0, &mut t);
                    add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&t), zt, W_PIN);
                    // (M3: and its knots past the nose, inside the trunk's surface, at the trunk's height there: free,
                    // they carried a climbing ramp's grade on 2.6 m over the motorway it joins, Ottawa 417)
                    for (i, ts) in cut_knots(net, si, start, &sn, h[si], m, h[sn.trunk]) {
                        add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&[(i, 1.0)]), at_knots(&z[sn.trunk], h[sn.trunk], ts), W_PIN);
                    }
                    continue;
                }
            }
            if pin_node(node) { add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&[(kv, 1.0)]), node_z[node], W_PIN); }
        }
        let (zz, key) = qp.solve(&mut stats);
        qp.check(&zz, &mut ProfStats::default());
        z[si] = zz;
        keys[si] = key;
        stats.tier_a += 1;
        stats.knots += m;
        comp_cpu[si] = crate::cpu_now() - t0;
    }
    let tier_a = z.clone();
    // ---- Tier B sections (the approach rule): plan(), then one solve per component
    let pl = plan_w(net, need, window);
    stats.cap_cuts = pl.cap_cuts;
    stats.sections = pl.secs.iter().filter(|x| !x.secondary).count();
    stats.secondary = pl.secs.iter().filter(|x| x.secondary).count();
    let (secs, cpl, joined, root) = (&pl.secs, &pl.cpl, &pl.joined, &pl.root);
    let nsec = secs.len();
    let sec_at = |si: usize, s: f64| pl.sec_at(si, s);
    let debug_floors = std::env::var("ROADSYS_DEBUG_FLOORS").is_ok();     // (stderr only: unmet floors listed)
    let mut members: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for i in 0..nsec { members.entry(root[i]).or_default().push(i); }
    let mut comp_of = vec![usize::MAX; ns];
    let mut sections: Vec<Vec<(f64, f64, usize)>> = vec![Vec::new(); ns];
    for (cid, (_r, mem)) in members.iter().enumerate() {
        let t0 = crate::cpu_now();
        stats.components += 1;
        stats.tier_b += 1;
        let mut spans: Vec<Span> = Vec::new();
        let mut n = 0;
        let mut ckm = 0.0;
        for &k in mem {
            let x = &secs[k];
            spans.push(Span { si: x.si, i0: x.i0, i1: x.i1, base: n });
            n += x.i1 - x.i0 + 1;
            ckm += (x.s1 - x.s0) / 1000.0;
        }
        if n > stats.biggest_component_knots { stats.biggest_component_knots = n; stats.biggest_component_km = ckm; }
        let var_of = |si: usize, i: usize| -> Option<usize> { spans.iter().find(|sp| sp.si == si && i >= sp.i0 && i <= sp.i1).map(|sp| sp.base + i - sp.i0) };
        let mut guess = vec![0.0; n];
        for sp in &spans { for i in sp.i0..=sp.i1 { guess[sp.base + i - sp.i0] = tier_a[sp.si][i]; } }
        let mut qp = Qp { n, trip: Vec::new(), rhs: vec![0.0; n], cons: Vec::new(), guess };
        add_spans(net, &spans, &h, &g, &ob, &mut qp);
        // stations where a coupling of this component holds a span (no Tier A pin within 3 knots of them)
        let mut held: Vec<(usize, f64)> = Vec::new();
        for (k, c) in cpl.iter().enumerate() {
            if !(joined[k] && root[c.a] == root[mem[0]]) { continue; }
            match c.what {
                0 => { let cr = &net.crossings[c.idx]; held.push((cr.a, cr.sa)); held.push((cr.b, cr.sb)); }
                1 => { for &(o, a) in &ends[c.idx] { held.push((o, if a { 0.0 } else { net.strokes[o].len() })); } }
                _ => {
                    let (si, start) = (c.idx / 2, c.idx % 2 == 1);
                    let st = &net.strokes[si];
                    if let Some(sn) = if start { st.snap0 } else { st.snap1 } { held.push((si, snap_station(si, start))); held.push((sn.trunk, sn.s)); }
                }
            }
        }
        let is_held = |si: usize, s: f64| held.iter().any(|&(o, x)| o == si && absf(x - s) <= 3.0 * h[si]);
        // inner ends held to Tier A (height and slope); stroke ends at a node not coupled here: their Tier A end
        for sp in &spans {
            let m = nk[sp.si];
            let st = &net.strokes[sp.si];
            for (at_lo, i_end) in [(true, sp.i0), (false, sp.i1)] {
                let held = is_held(sp.si, (i_end as f64 * h[sp.si]).min(st.len()));
                let stroke_end = if at_lo { sp.i0 == 0 } else { sp.i1 == m - 1 };
                if !stroke_end && held { continue; }
                if stroke_end {
                    let node = if at_lo { st.n0 } else { st.n1 };
                    let start = at_lo;
                    let coupled = ends[node].len() >= 2 && ends[node].iter().all(|&(o, a)| {
                        (o, a) == (sp.si, start) || sec_at(o, if a { 0.0 } else { net.strokes[o].len() }).map(|k| root[k] == root[mem[0]]).unwrap_or(false)
                    });
                    let snapped = if start { st.snap0.is_some() } else { st.snap1.is_some() };
                    if coupled {
                        if held { continue; }
                        if pin_node(node) { add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&[(var_of(sp.si, i_end).unwrap(), 1.0)]), node_z[node], 10.0 * h[sp.si]); }
                        continue;
                    }
                    // (M2: a node end not coupled to all its arms is held at Tier A whatever else holds it here: the arms
                    // left out stay there; only a snapped end's own nose or joint may hold it instead)
                    if snapped && held { continue; }
                    if snapped || pin_node(node) || ends[node].len() >= 2 {
                        add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&[(var_of(sp.si, i_end).unwrap(), 1.0)]), tier_a[sp.si][i_end], W_PIN);
                    }
                    continue;
                }
                let i2 = if at_lo { i_end + 1 } else { i_end - 1 };
                for i in [i_end, i2] {
                    add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&[(var_of(sp.si, i).unwrap(), 1.0)]), tier_a[sp.si][i], W_PIN);
                }
            }
        }
        // couplings inside the component; floors (one-sided against the lower road's Tier A where it stays out)
        let mut couples: Vec<Lin> = Vec::new();
        let mut couple_dbg: Vec<String> = Vec::new();
        let mut floor_dbg: Vec<(usize, String)> = Vec::new();
        for (k, c) in cpl.iter().enumerate() {
            let in_a = root[c.a] == root[mem[0]];
            let in_b = root[c.b] == root[mem[0]];
            if !(in_a || in_b) { continue; }
            match c.what {
                0 => {
                    let cr = &net.crossings[c.idx];
                    let (up, us, lo, ls_) = if cr.upper == cr.a { (cr.a, cr.sa, cr.b, cr.sb) } else { (cr.b, cr.sb, cr.a, cr.sa) };
                    let up_in = sec_at(up, us).map(|x| root[x] == root[mem[0]]).unwrap_or(false);
                    let lo_in = sec_at(lo, ls_).map(|x| root[x] == root[mem[0]]).unwrap_or(false);
                    if !up_in { continue; }
                    let mut t = Vec::new();
                    // (M3: a floor between two decks both measured there at heights it would part (lidar, BD TOPO,
                    // NVDB): the measurement is the truth, no floor; twin decks at two OSM layers, Baltimore I-95)
                    // (the nearest measured knot within MEASURED_NEAR_M: a lower deck's own measurements are left out
                    // where another deck runs over it)
                    let ob_at = |si: usize, s: f64| -> f64 {
                        let o = &ob[si];
                        if o.is_empty() { return f64::NAN; }
                        let c = ((s / h[si]).round() as usize).min(o.len() - 1);
                        let r = (MEASURED_NEAR_M / h[si]).ceil() as usize;
                        for d in 0..=r {
                            if c >= d && o[c - d].is_finite() { return o[c - d]; }
                            if c + d < o.len() && o[c + d].is_finite() { return o[c + d]; }
                        }
                        f64::NAN
                    };
                    let (ou, ol) = (ob_at(up, us), ob_at(lo, ls_));
                    if ou.is_finite() && ol.is_finite() && ou - ol < cr.floor - MEASURED_APART_M { stats.floors_measured_out += 1; continue; }
                    let su = sec_at(up, us).unwrap();
                    let spu = spans.iter().find(|sp| sp.si == up && sp.i0 == secs[su].i0).copied().unwrap();
                    lin_sec(&spu, h[up], us, 1.0, &mut t);
                    let mut bound = cr.floor;
                    if cr.stacked { bound -= DECK_DEPTH - STACK_DEPTH; stats.stacked += 1; }
                    let true_floor = bound;
                    // feasibility (M2): the most the grades can open between the two from their sections' held ends
                    // (the upper climbing from each end at its class limit, the lower falling from each of its own);
                    // a floor past it is relaxed to it and recorded (an unreachable floor fought the pins: ramps)
                    let ach_hi = { let (i0, i1) = (spu.i0, spu.i1); let gl = grade_limit(net, up);
                        let (a, b) = (tier_a[up][i0] + gl * (us - i0 as f64 * h[up]).max(0.0), tier_a[up][i1] + gl * (i1 as f64 * h[up] - us).max(0.0));
                        if a < b { a } else { b } };
                    let joint = lo_in && joined[k];
                    let lo_floor;
                    if joint {
                        let sl = sec_at(lo, ls_).unwrap();
                        let spl = spans.iter().find(|sp| sp.si == lo && sp.i0 == secs[sl].i0).copied().unwrap();
                        lin_sec(&spl, h[lo], ls_, -1.0, &mut t);
                        let gl = grade_limit(net, lo);
                        let (a, b) = (tier_a[lo][spl.i0] - gl * (ls_ - spl.i0 as f64 * h[lo]).max(0.0), tier_a[lo][spl.i1] - gl * (spl.i1 as f64 * h[lo] - ls_).max(0.0));
                        let mut lf = if a > b { a } else { b };
                        if net.strokes[lo].support_at(ls_).kind == SUP_GROUND {
                            let gd = at_knots(&g[lo], h[lo], ls_) - DIG_MAX;
                            if gd > lf { lf = gd; }
                        }
                        lo_floor = lf;
                    } else {
                        if tier_a[lo].is_empty() { continue; }      // (the lower road is not built here)
                        lo_floor = at_knots(&tier_a[lo], h[lo], ls_);
                        bound += lo_floor;
                    }
                    let reach = ach_hi - lo_floor - FLOOR_SLACK;
                    let mut orig = f64::NAN;
                    if true_floor > reach {
                        let r = true_floor - reach.max(0.0);
                        stats.floors_relaxed += 1;
                        if r > stats.floor_relax_max_m { stats.floor_relax_max_m = r; }
                        orig = bound;
                        bound -= r;
                    }
                    if debug_floors {
                        let (su, sl) = (net.strokes[up].support_at(us), net.strokes[lo].support_at(ls_));
                        floor_dbg.push((qp.cons.len(), format!("up cls {} sup {} kmh {} s {:.0}/{:.0} | lo cls {} sup {} s {:.0}/{:.0} | floor {:.2} joined {} lo_in {} p {:.0},{:.0}",
                            net.strokes[up].cls, su.kind, net.strokes[up].kmh, us, net.strokes[up].len(), net.strokes[lo].cls, sl.kind, ls_, net.strokes[lo].len(), cr.floor, joined[k], lo_in, cr.p[0], cr.p[1])));
                    }
                    qp.cons.push(Ineq { f: mklin(&t), bound, ge: true, w: W_FLOOR, kind: 4, orig });
                }
                _ if !(in_a && in_b && joined[k]) => {}
                1 => {
                    let nd = c.idx;
                    let e: Vec<(usize, bool)> = ends[nd].iter().copied().filter(|&(si, a)| want(si) && sec_at(si, if a { 0.0 } else { net.strokes[si].len() }).is_some()).collect();
                    let (k0, k1) = (&e[0], e.iter().find(|x| net.strokes[x.0].uid == c.key.2 && x.1 as i64 == c.key.4).unwrap());
                    let va = var_of(k0.0, if k0.1 { 0 } else { nk[k0.0] - 1 });
                    let vb = var_of(k1.0, if k1.1 { 0 } else { nk[k1.0] - 1 });
                    if let (Some(va), Some(vb)) = (va, vb) { couples.push(mklin(&[(va, 1.0), (vb, -1.0)])); couple_dbg.push(format!("node {} at {:?} deck {}", nd, net.nodes[nd].p, deck_node[nd])); }
                }
                _ => {
                    let (si, start) = (c.idx / 2, c.idx % 2 == 1);
                    let st = &net.strokes[si];
                    let sn = if start { st.snap0 } else { st.snap1 }.unwrap();
                    let (sa, sb) = (sec_at(si, snap_station(si, start)).unwrap(), sec_at(sn.trunk, sn.s).unwrap());
                    let spa = spans.iter().find(|sp| sp.si == si && sp.i0 == secs[sa].i0).copied().unwrap();
                    let spb = spans.iter().find(|sp| sp.si == sn.trunk && sp.i0 == secs[sb].i0).copied().unwrap();
                    let mut t = Vec::new();
                    lin_sec(&spa, h[si], snap_station(si, start), 1.0, &mut t);
                    lin_sec(&spb, h[sn.trunk], sn.s, -1.0, &mut t);
                    couples.push(mklin(&t));
                    couple_dbg.push(format!("nose branch {} trunk {} at {:?} joint {}", si, sn.trunk, net.strokes[si].line.at(snap_station(si, start)).0, sn.joint));
                }
            }
        }
        // (M3) a branch's knots past its nose lie on its trunk's surface: coupled to the trunk where the trunk is in
        // this component, else held at the trunk's Tier A
        for sp in &spans {
            let st = &net.strokes[sp.si];
            for (start, sn) in [(true, st.snap0), (false, st.snap1)] {
                let Some(sn) = sn else { continue };
                if tier_a[sn.trunk].is_empty() { continue; }
                for (i, ts) in cut_knots(net, sp.si, start, &sn, h[sp.si], nk[sp.si], h[sn.trunk]) {
                    if i < sp.i0 || i > sp.i1 { continue; }
                    let v = sp.base + i - sp.i0;
                    let ht = h[sn.trunk];
                    match spans.iter().find(|q| q.si == sn.trunk && ts >= q.i0 as f64 * ht - 1e-6 && ts <= q.i1 as f64 * ht + 1e-6) {
                        Some(q) => { let mut t = vec![(v, 1.0)]; lin_sec(q, ht, ts, -1.0, &mut t); couples.push(mklin(&t)); couple_dbg.push(format!("cut knot {} of {}", i, sp.si)); }
                        None => add_sq(&mut qp.trip, &mut qp.rhs, &mklin(&[(v, 1.0)]), at_knots(&tier_a[sn.trunk], ht, ts), W_PIN),
                    }
                }
            }
        }
        for f in &couples { add_sq(&mut qp.trip, &mut qp.rhs, f, 0.0, W_COUPLE); }
        let (zz, key) = qp.solve(&mut stats);
        qp.check(&zz, &mut stats);
        if std::env::var("ROADSYS_DEBUG_COMFORT").is_ok() {
            for c in &qp.cons {
                if c.kind != 2 { continue; }
                let v = c.f.val(&zz);
                let short = if c.ge { c.bound - v } else { v - c.bound };
                if !(short > 0.5 * absf(c.bound) + 0.002) { continue; }
                let var = c.f.t[1].0;
                let sp = spans.iter().find(|sp| var >= sp.base && var <= sp.base + sp.i1 - sp.i0).unwrap();
                let i = sp.i0 + var - sp.base;
                let st = &net.strokes[sp.si];
                let s = (i as f64 * h[sp.si]).min(st.len());
                let d_end = (i - sp.i0).min(sp.i1 - i);
                let held_here = is_held(sp.si, s);
                let d_sup = st.sup.iter().map(|u| absf(s - u.s0).min(absf(s - u.s1))).fold(f64::INFINITY, f64::min);
                let sup = st.support_at(s).kind;
                eprintln!("comfort cls {} kmh {} sup {} d_end {} held {} d_sup {:.0} short {:.4} bound {:.4} g {:.2} z {:.2} ta {:.2}", st.cls, st.kmh, sup, d_end, held_here, d_sup, short, c.bound, g[sp.si][i], zz[var], tier_a[sp.si][i]);
            }
        }
        for (k, msg) in &floor_dbg {
            let c = &qp.cons[*k];
            let short = c.bound - c.f.val(&zz);
            if short > 0.05 { eprintln!("floor short {:.2} | {}", short, msg); }
        }
        for (ci, f) in couples.iter().enumerate() {
            let e = absf(f.val(&zz)) * 1000.0;
            if e > stats.couple_max_mm { stats.couple_max_mm = e; }
            if e > 100.0 && debug_floors { eprintln!("coupling {} off {:.0} mm: {:?}", ci, e, &couple_dbg[ci]); }
        }
        stats.knots += n;
        let dt = crate::cpu_now() - t0;
        for sp in &spans {
            for i in sp.i0..=sp.i1 { z[sp.si][i] = zz[sp.base + i - sp.i0]; }
            keys[sp.si] ^= key.rotate_left((sp.i0 % 61) as u32);
            if comp_of[sp.si] == usize::MAX { comp_of[sp.si] = cid; }
            sections[sp.si].push((secs.iter().find(|x| x.si == sp.si && x.i0 == sp.i0).map(|x| x.s0).unwrap_or(0.0), (sp.i1 as f64 * h[sp.si]).min(net.strokes[sp.si].len()), cid));
            comp_cpu[sp.si] += dt;
        }
    }
    Profiles { h, z, comp_of, comp_cpu, keys, sections, stats, g, ob, tier_a }
}

/// (M3) A snapped stroke's knots past its nose (inside its trunk's surface: the first cut0 m at a start snap, the last
/// cut1 m at an end snap), each with the trunk station its point projects to (searched round the nose's station).
fn cut_knots(net: &Net, si: usize, start: bool, sn: &crate::net::Snap, h: f64, m: usize, ht: f64) -> Vec<(usize, f64)> {
    let st = &net.strokes[si];
    let len = st.len();
    let cut = if start { st.cut0 } else { st.cut1 };
    let mut out = Vec::new();
    if !(cut > 1e-3) || m < 2 { return out; }
    let tr = &net.strokes[sn.trunk];
    let (t0, t1) = ((sn.s - cut - 60.0).max(0.0), (sn.s + cut + 60.0).min(tr.len()));
    for i in 0..m {
        let s = (i as f64 * h).min(len);
        let inside = if start { s < cut - 1e-6 } else { s > len - cut + 1e-6 };
        if !inside { continue; }
        let (ts, _off) = tr.line.project(st.line.at(s).0, t0, t1);
        let _ = ht;
        out.push((i, ts));
    }
    out
}

fn at_knots(z: &[f64], h: f64, s: f64) -> f64 {
    if z.is_empty() { return 0.0; }
    if z.len() == 1 { return z[0]; }
    let f = clampf(s / h, 0.0, (z.len() - 1) as f64);
    let i = (f as usize).min(z.len() - 2);
    let u = f - i as f64;
    z[i] * (1.0 - u) + z[i + 1] * u
}

/// The form of station s on a span (linear between its knots, clamped to the span).
fn lin_sec(sp: &Span, h: f64, s: f64, sign: f64, t: &mut Vec<(usize, f64)>) {
    let f = clampf(s / h, sp.i0 as f64, sp.i1 as f64);
    let i = (f as usize).clamp(sp.i0, sp.i1.saturating_sub(1).max(sp.i0));
    let u = f - i as f64;
    if sp.i1 == sp.i0 { t.push((sp.base, sign)); return; }
    t.push((sp.base + i - sp.i0, sign * (1.0 - u)));
    t.push((sp.base + i + 1 - sp.i0, sign * u));
}

#[cfg(test)]
mod tests {
    use super::*;

    /// (M3) The active set lets a limit go once the solution leaves it on its feasible side: x0 >= 1 and x0 - x1 >= 3
    /// with both pulled to 0 end at (1.5, -1.5) (only the second binds), not with x0 held at 1 as penalty rounds kept it.
    #[test]
    fn a_limit_left_slack_leaves_the_active_set() {
        let mut qp = Qp { n: 2, trip: vec![(0, 0, 1.0), (1, 1, 1.0)], rhs: vec![0.0, 0.0], cons: Vec::new(), guess: vec![0.0, 0.0] };
        qp.cons.push(Ineq { f: mklin(&[(0, 1.0)]), bound: 1.0, ge: true, w: 1e7, kind: 1, orig: f64::NAN });
        qp.cons.push(Ineq { f: mklin(&[(0, 1.0), (1, -1.0)]), bound: 3.0, ge: true, w: 1e7, kind: 1, orig: f64::NAN });
        store_clear();
        let (z, _) = qp.solve(&mut ProfStats::default());
        assert!(absf(z[0] - 1.5) < 2e-3 && absf(z[1] + 1.5) < 2e-3, "{:?}", z);
    }
}
