//! M3 (BNE_ROADSYS=heights): a bridge chain's decks meet the approach fills the plans already have.
//!
//! The profile squares give each deck its heights from one profile per carriageway; the plans' first stage gives the
//! ground roads their own (approach fills included). Each free end of a chain (an abutment: every part there leaves
//! it to one side) is brought onto the ground the plans give just past it, lifted or dropped (within lift_max /
//! drop_max); the joints take a harmonic blend of the free ends' changes (weights 1 / the part's length); along each
//! part the change is linear by arc length, except that a drop reaches no further than the part's nearest hold (the
//! floors it holds over other roads: piecewise linear through 0 there), so a floor the squares met stays met. The
//! ground at the abutments stays the plans' (roadsys.pins_near): deck end and ground agree.
//!
//! Pure: IEEE + - * / and sqrt only, fixed order (parts as given, nodes in order of first appearance, a fixed number of
//! Gauss-Seidel sweeps).
use crate::geom::*;

pub const JOIN_M: f64 = 0.75;       // part ends this close are one node (roads.plan_bridges_joined's 0.75 m)
const SWEEPS: usize = 400;

/// Per part (points, heights) and the plans' ground just past each of its two ends (NaN: not read): the heights with
/// the chain lifted onto its abutments' ground (see the module's notes). Returns the lifts per part vertex.
pub fn lifts(parts: &[(Vec<P2>, Vec<f64>)], ends: &[(f64, f64)], lift_max: f64, drop_max: f64) -> Vec<Vec<f64>> {
    lifts_held(parts, ends, lift_max, drop_max, &vec![Vec::new(); parts.len()])
}

/// lifts() with holds per part (arc stations of the floors it holds over other roads): where a part's end is dropped,
/// the drop reaches no further than its nearest hold (piecewise linear through 0 at every hold): a floor met by the
/// profile stays met.
pub fn lifts_held(parts: &[(Vec<P2>, Vec<f64>)], ends: &[(f64, f64)], lift_max: f64, drop_max: f64, holds: &[Vec<f64>]) -> Vec<Vec<f64>> {
    // nodes: part ends merged within JOIN_M, in order of first appearance
    let mut nodes: Vec<P2> = Vec::new();
    let mut count: Vec<usize> = Vec::new();
    let mut of: Vec<[usize; 2]> = Vec::with_capacity(parts.len());
    let mut dirs: Vec<Vec<P2>> = Vec::new();      // per node: each part's direction leaving it
    for (pts, _z) in parts {
        let mut ix = [usize::MAX; 2];
        if pts.len() >= 2 {
            let m = pts.len();
            for (k, (p, q)) in [(pts[0], pts[1]), (pts[m - 1], pts[m - 2])].into_iter().enumerate() {
                let found = nodes.iter().position(|r| norm(sub(*r, p)) <= JOIN_M);
                let n = match found { Some(n) => n, None => { nodes.push(p); count.push(0); dirs.push(Vec::new()); nodes.len() - 1 } };
                count[n] += 1;
                dirs[n].push(unit(sub(q, p)).unwrap_or([1.0, 0.0]));
                ix[k] = n;
            }
        }
        of.push(ix);
    }
    // a node is an abutment (free) when every part there leaves it to one side (one part, or twins merging onto the
    // ground road there); a joint has parts on both sides
    let free: Vec<bool> = dirs.iter().map(|ds| ds.iter().all(|a| ds.iter().all(|b| dot(*a, *b) > 0.0))).collect();
    let nn = nodes.len();
    // the free ends' shortfalls (a node with one part end: an abutment)
    let mut fixed = vec![false; nn];
    let mut c = vec![0.0f64; nn];
    for (i, (pts, z)) in parts.iter().enumerate() {
        if pts.len() < 2 || z.len() != pts.len() { continue; }
        for k in 0..2 {
            let n = of[i][k];
            if !free[n] { continue; }
            let g = if k == 0 { ends[i].0 } else { ends[i].1 };
            let zz = if k == 0 { z[0] } else { z[z.len() - 1] };
            let v = if g.is_finite() { clampf(g - zz, -drop_max, lift_max) } else { 0.0 };
            // (twins at one abutment: the most any of them asks)
            if !fixed[n] || absf(v) > absf(c[n]) { c[n] = v; }
            fixed[n] = true;
        }
    }
    if fixed.iter().all(|f| !f) || c.iter().all(|&v| v == 0.0) {
        return parts.iter().map(|(p, _)| vec![0.0; p.len()]).collect();
    }
    // joints: harmonic over the chain's graph (weights 1 / length), Gauss-Seidel in node order
    let len: Vec<f64> = parts.iter().map(|(p, _)| { let mut l = 0.0; for w in p.windows(2) { l += norm(sub(w[1], w[0])); } l.max(1e-3) }).collect();
    for _ in 0..SWEEPS {
        for n in 0..nn {
            if fixed[n] { continue; }
            let (mut sw, mut sv) = (0.0f64, 0.0f64);
            for (i, ix) in of.iter().enumerate() {
                if ix[0] == usize::MAX { continue; }
                for k in 0..2 {
                    if ix[k] != n { continue; }
                    let o = ix[1 - k];
                    if o == n { continue; }
                    let w = 1.0 / len[i];
                    sw += w;
                    sv += w * c[o];
                }
            }
            if sw > 0.0 { c[n] = sv / sw; }
        }
    }
    parts.iter().enumerate().map(|(i, (pts, _))| {
        if pts.len() < 2 { return vec![0.0; pts.len()]; }
        let (c0, c1) = (c[of[i][0]], c[of[i][1]]);
        // anchors: its two ends, and (where an end drops) 0 at each hold
        let mut anc: Vec<(f64, f64)> = vec![(0.0, c0)];
        if c0 < 0.0 || c1 < 0.0 {
            let mut hs: Vec<f64> = holds.get(i).cloned().unwrap_or_default().into_iter().filter(|&h| h > 0.0 && h < len[i]).collect();
            hs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            for h in hs { anc.push((h, 0.0)); }
        }
        anc.push((len[i], c1));
        let mut s = 0.0;
        let mut out = Vec::with_capacity(pts.len());
        let mut k = 0usize;
        for j in 0..pts.len() {
            if j > 0 { s += norm(sub(pts[j], pts[j - 1])); }
            while k + 2 < anc.len() && s > anc[k + 1].0 { k += 1; }
            let (a, b) = (anc[k], anc[k + 1]);
            let u = if b.0 > a.0 { clampf((s - a.0) / (b.0 - a.0), 0.0, 1.0) } else { 1.0 };
            out.push(a.1 + (b.1 - a.1) * u);
        }
        out
    }).collect()
}

/// (M5 round 8) The parameters of kept_ends.
#[derive(Clone, Copy)]
pub struct KeepPrm {
    pub short_m: f64,       // m: a chain this long at most (its parts' lengths summed) may keep an end ...
    pub dz_m: f64,          // ... one whose approach ground lies more than this off the profile's end there ...
    pub ramp_min_m: f64,    // ... where its approach has room for a ramp of |dz| / the class grade (this long at least)
    pub clear_m: f64,       // ... plus this, short of its first junction or structure (approach_run) ...
    pub ride_a: f64,        // ... and long enough that its curve (ramp_dz's smoothstep, z'' at most 6 r / L^2) takes at
                            // most this vertical acceleration (m/s^2) at the road's design speed (speeds)
}

/// (M5 round 8, road shape first) Per part, per end: (the length of the ramp its approach takes, the part of the
/// mismatch the end is still brought by) where the chain keeps the profile's end there, wholly or in part (NaN:
/// brought onto its approach ground as before, lifts_held). A free end (one part end alone at its node: an abutment,
/// not twins merging) of a short chain (its parts' lengths summed at most prm.short_m) whose approach ground (ends;
/// NaN: not read) lies d > prm.dz_m off its profile height takes on its approach as much of d as a ramp at its class
/// grade (grades, per part) and within prm.ride_a at its design speed (speeds, m/s) fits into the approach's room
/// (runs: m free of junctions and structures from the read point; NaN: none) less prm.clear_m: r = min(d, grade x R,
/// ride_a R^2 / (6 v^2)) for R that room, when R is prm.ramp_min_m at least and r is all of d (more than prm.dz_m: an
/// end kept in part made lips at Stelvio); the ramp is max(prm.ramp_min_m, r / grade, v sqrt(6 r / ride_a)) long (the lift 0); and
/// only where the part comes out gentler than brought wholly onto its ground. The profile squares solve the deck
/// within its class's grades over ground and structure together; lifting a
/// short deck's end onto an approach ground lying in a hollow of the elevation (a surface model's filtered
/// embankment, a scree cone over a covered section) steepened it to 18-45 % (0bff, Stelvio, M5 round 7): such an end
/// stays (as far as its approach can be ramped to it: roadsys.pins_near). Pure.
pub fn kept_ends(parts: &[(Vec<P2>, Vec<f64>)], ends: &[(f64, f64)], runs: &[(f64, f64)], grades: &[f64], speeds: &[f64],
                 prm: &KeepPrm) -> Vec<[f64; 4]> {
    let mut out = vec![[f64::NAN; 4]; parts.len()];
    let mut nodes: Vec<P2> = Vec::new();
    let mut count: Vec<usize> = Vec::new();
    let mut dirs: Vec<Vec<P2>> = Vec::new();
    let mut of: Vec<[usize; 2]> = Vec::with_capacity(parts.len());
    let mut total = 0.0f64;
    for (pts, _z) in parts {
        let mut ix = [usize::MAX; 2];
        if pts.len() >= 2 {
            total += arc_len(pts);
            let m = pts.len();
            for (k, (p, q)) in [(pts[0], pts[1]), (pts[m - 1], pts[m - 2])].into_iter().enumerate() {
                let found = nodes.iter().position(|r| norm(sub(*r, p)) <= JOIN_M);
                let n = match found { Some(n) => n, None => { nodes.push(p); count.push(0); dirs.push(Vec::new()); nodes.len() - 1 } };
                count[n] += 1;
                dirs[n].push(unit(sub(q, p)).unwrap_or([1.0, 0.0]));
                ix[k] = n;
            }
        }
        of.push(ix);
    }
    if !(total <= prm.short_m) { return out; }
    let free: Vec<bool> = dirs.iter().map(|ds| ds.iter().all(|a| ds.iter().all(|b| dot(*a, *b) > 0.0))).collect();
    for (i, (pts, z)) in parts.iter().enumerate() {
        if pts.len() < 2 || z.len() != pts.len() || i >= ends.len() || i >= runs.len() || i >= grades.len()
            || i >= speeds.len() { continue; }
        let (gr, v) = (grades[i], speeds[i]);
        if !(gr > 0.0) || !(v > 0.0) || !(prm.ride_a > 0.0) { continue; }
        for k in 0..2 {
            let n = of[i][k];
            if n == usize::MAX || !free[n] || count[n] != 1 { continue; }
            let g = if k == 0 { ends[i].0 } else { ends[i].1 };
            let run = if k == 0 { runs[i].0 } else { runs[i].1 };
            let zz = if k == 0 { z[0] } else { z[z.len() - 1] };
            if !g.is_finite() || !run.is_finite() { continue; }
            let d = absf(g - zz);
            let room = run - prm.clear_m;
            if !(d > prm.dz_m) || !(room >= prm.ramp_min_m) { continue; }
            let r = d.min(gr * room).min(prm.ride_a * room * room / (6.0 * v * v));
            // (wholly or not at all: an end kept in part, its deck still brought most of the way, left Stelvio's 33 m
            // deck at 37 % for lips over 20 mm 35 -> 80 along its far end's edge)
            if !(r > prm.dz_m) || r < d { continue; }
            let sign = if g > zz { 1.0 } else { -1.0 };
            out[i][2 * k] = (r / gr).max(prm.ramp_min_m).max(v * (6.0 * r / prm.ride_a).sqrt());
            out[i][2 * k + 1] = sign * (d - r);
        }
        // (kept only where the part comes out gentler than brought wholly onto its ground: one end kept at a profile
        // far off the ground while the other is brought down to it made a 12 m track deck 160 %, Stelvio)
        if out[i][0].is_finite() || out[i][2].is_finite() {
            let at = |k: usize, kept: bool| -> f64 {
                let g = if k == 0 { ends[i].0 } else { ends[i].1 };
                let zz = if k == 0 { z[0] } else { z[z.len() - 1] };
                let n = of[i][k];
                let free_end = n != usize::MAX && free[n];
                if kept && out[i][2 * k].is_finite() { zz + out[i][2 * k + 1] } else if free_end && g.is_finite() { g } else { zz }
            };
            let len = arc_len(pts).max(1e-3);
            let full = absf(at(1, false) - at(0, false)) / len;
            let keep = absf(at(1, true) - at(0, true)) / len;
            if !(keep < full) { out[i] = [f64::NAN; 4]; }
        }
    }
    out
}

/// (M5 round 8) One road line of approach_run's neighbourhood: its points, each connector on it (arc m from its first
/// point, node id) and the arcs where a structure or tunnel range of it begins or ends (stops).
pub struct RunLine { pub pts: Vec<P2>, pub nodes: Vec<(f64, u64)>, pub stops: Vec<f64> }

const RUN_EPS: f64 = 0.05;      // m: a connector this near a line's end is at its end; one ahead lies past this

fn arc_len(p: &[P2]) -> f64 {
    let mut l = 0.0;
    for w in p.windows(2) { l += norm(sub(w[1], w[0])); }
    l
}

fn point_at(p: &[P2], s: f64) -> P2 {
    let mut a = 0.0;
    for w in p.windows(2) {
        let l = norm(sub(w[1], w[0]));
        if l > 0.0 && a + l >= s {
            let u = clampf((s - a) / l, 0.0, 1.0);
            return [w[0][0] + u * (w[1][0] - w[0][0]), w[0][1] + u * (w[1][1] - w[0][1])];
        }
        a += l;
    }
    p[p.len() - 1]
}

/// (M5 round 8) A deck end's approach from arc s0 of line li, walking forward (fwd) or back along it: (m of it free of
/// junctions and structures, at most max_m; its points every <= 2 m over that). A connector ahead ends it (a
/// junction) unless it is the line's own end and exactly one other line meets it there, at that line's own end (the
/// road going on: the walk goes on along it); a structure's or tunnel's range boundary ahead ends it too, as does a
/// line ending with no connector. Pure.
pub fn approach_run(lines: &[RunLine], li: usize, s0: f64, fwd: bool, max_m: f64) -> (f64, Vec<P2>) {
    let mut refs: std::collections::BTreeMap<u64, Vec<(usize, f64)>> = std::collections::BTreeMap::new();
    for (i, l) in lines.iter().enumerate() {
        for &(s, n) in &l.nodes { refs.entry(n).or_default().push((i, s)); }
    }
    let (mut li, mut s, mut fwd) = (li, s0, fwd);
    let mut walked = 0.0f64;
    let mut pts: Vec<P2> = Vec::new();
    for _hop in 0..64 {
        if li >= lines.len() || lines[li].pts.len() < 2 { break; }
        let l = &lines[li];
        let len = arc_len(&l.pts);
        let dist = |x: f64| if fwd { x - s } else { s - x };
        let room = dist(if fwd { len } else { 0.0 }).max(0.0);
        // (the nearest event ahead: 0 the line's end, 1 a connector, 2 a structure's boundary)
        let (mut ev_d, mut kind, mut node) = (room, 0u8, 0u64);
        for &(x, n) in &l.nodes {
            let d = dist(x);
            // (behind or at the start; but a walk starting at the line's end takes the connector there: the road
            // goes on past a deck end at its line's end)
            if d <= RUN_EPS && !(room <= RUN_EPS && absf(d - room) <= RUN_EPS) { continue; }
            let d = if d >= room - RUN_EPS { room } else { d };
            if d < ev_d || (d == ev_d && kind == 0) { ev_d = d; kind = 1; node = n; }
        }
        for &x in &l.stops {
            let d = dist(x);
            if d > RUN_EPS && d < ev_d { ev_d = d; kind = 2; }
        }
        let take = ev_d.min(max_m - walked).max(0.0);
        let k = ((take / 2.0).ceil() as usize).max(1);
        for j in 0..=k {
            if j == 0 && !pts.is_empty() { continue; }
            let d = take * j as f64 / k as f64;
            pts.push(point_at(&l.pts, if fwd { s + d } else { s - d }));
        }
        walked += take;
        if walked >= max_m { return (max_m, pts); }
        if kind != 1 || ev_d < room { return (walked, pts); }
        // (the line's end at a connector: on over a plain continuation)
        let others: Vec<(usize, f64)> = refs.get(&node).map(|v| v.iter().copied().filter(|&(i, _)| i != li).collect()).unwrap_or_default();
        if others.len() != 1 { return (walked, pts); }
        let (oi, os) = others[0];
        if oi >= lines.len() || lines[oi].pts.len() < 2 { return (walked, pts); }
        let olen = arc_len(&lines[oi].pts);
        if os <= RUN_EPS { fwd = true; } else if olen - os <= RUN_EPS { fwd = false; } else { return (walked, pts); }
        li = oi;
        s = if fwd { 0.0 } else { olen };
    }
    (walked, pts)
}

/// (M5 round 8) A kept end's approach ramp (roadsys.pins_near): at each arc m s along the approach from its read point,
/// the change to the plans' ground there: dz0 (the deck end's height less that ground at the read point) times a
/// smoothstep from 1 at s = 0 to 0 at s = ramp (0 past it). Pure.
pub fn ramp_dz(s: &[f64], dz0: f64, ramp: f64) -> Vec<f64> {
    s.iter().map(|&x| {
        let f = if ramp > 0.0 { clampf(1.0 - x / ramp, 0.0, 1.0) } else { 0.0 };
        dz0 * (f * f * (3.0 - 2.0 * f))
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_chain_is_lifted_onto_its_abutments_and_never_dropped() {
        // two parts joined at x = 50: the west abutment's ground 1 m over the deck end, the east one's under it
        let a = (vec![[0.0, 0.0], [25.0, 0.0], [50.0, 0.0]], vec![10.0, 10.5, 11.0]);
        let b = (vec![[50.0, 0.0], [100.0, 0.0]], vec![11.0, 10.0]);
        let l = lifts(&[a.clone(), b.clone()], &[(11.0, f64::NAN), (f64::NAN, 9.0)], 5.0, 0.0);
        assert!((l[0][0] - 1.0).abs() < 1e-9);
        assert!((l[1][1] - 0.0).abs() < 1e-9, "never dropped: {:?}", l);
        // the joint: harmonic, weights 1/50 both sides -> the mean 0.5, the same on both parts
        assert!((l[0][2] - 0.5).abs() < 1e-6 && (l[1][0] - 0.5).abs() < 1e-6, "{:?}", l);
        assert!((l[0][1] - 0.75).abs() < 1e-6);
        // nothing short: no lift
        let l = lifts(&[a.clone(), b.clone()], &[(9.0, f64::NAN), (f64::NAN, 9.0)], 5.0, 0.0);
        assert!(l.iter().flatten().all(|&v| v == 0.0));
        // at most lift_max
        let l = lifts(&[a.clone(), b.clone()], &[(14.0, f64::NAN), (f64::NAN, 9.0)], 1.0, 0.0);
        assert!((l[0][0] - 1.0).abs() < 1e-9);
        // twin decks merging onto one abutment: both free there, lifted alike
        let c = (vec![[0.0, 3.0], [50.0, 0.5]], vec![10.0, 11.0]);
        let d = (vec![[0.0, -3.0], [50.0, 0.0]], vec![10.0, 11.0]);
        let l = lifts(&[c, d], &[(f64::NAN, 11.5), (f64::NAN, 11.2)], 5.0, 0.0);
        assert!((l[0][1] - 0.5).abs() < 1e-9 && (l[1][1] - 0.5).abs() < 1e-9, "{:?}", l);
        // a drop stops at the part's hold (a floor it holds there)
        let e = (vec![[0.0, 0.0], [20.0, 0.0], [40.0, 0.0], [100.0, 0.0]], vec![10.0, 10.5, 11.0, 12.0]);
        let l = lifts_held(&[e], &[(8.0, f64::NAN)], 5.0, 5.0, &[vec![40.0]]);
        assert!((l[0][0] + 2.0).abs() < 1e-9 && (l[0][1] + 1.0).abs() < 1e-9 && l[0][2].abs() < 1e-9 && l[0][3].abs() < 1e-9, "{:?}", l);
    }

    #[test]
    fn a_short_deck_keeps_an_end_its_approach_has_room_to_ramp_to() {
        let prm = KeepPrm { short_m: 60.0, dz_m: 0.75, ramp_min_m: 20.0, clear_m: 20.0, ride_a: 3.0 };
        let a = (vec![[0.0, 0.0], [32.0, 0.0]], vec![70.0, 69.7]);
        // the west end's ground 2 m under it (2 / 0.06 = 33.3 m of ramp + 20 m clear in 60 m free), the east's 0.5 m off
        let k = kept_ends(&[a.clone()], &[(68.0, 70.2)], &[(60.0, 60.0)], &[0.06], &[1.0], &prm);
        assert!((k[0][0] - 2.0 / 0.06).abs() < 1e-9 && k[0][1] == 0.0 && k[0][2].is_nan(), "{:?}", k);
        // a junction 45 m on: 25 m of ramp would take only 1.5 m of the 2: not kept (wholly or not at all)
        let k = kept_ends(&[a.clone()], &[(68.0, 70.2)], &[(45.0, 60.0)], &[0.06], &[1.0], &prm);
        assert!(k[0][0].is_nan(), "{:?}", k);
        // too little room before a junction: brought onto its ground as before
        assert!(kept_ends(&[a.clone()], &[(68.0, 70.2)], &[(35.0, 60.0)], &[0.06], &[1.0], &prm)[0][0].is_nan());
        // a long chain: never
        let b = (vec![[0.0, 0.0], [130.0, 0.0]], vec![70.0, 69.7]);
        assert!(kept_ends(&[b], &[(68.0, 66.0)], &[(500.0, 500.0)], &[0.06], &[1.0], &prm).iter().flatten().all(|v| v.is_nan()));
        // a small ramp: ramp_min_m at least
        let k = kept_ends(&[a.clone()], &[(69.0, f64::NAN)], &[(60.0, 60.0)], &[0.10], &[1.0], &prm);
        assert!((k[0][0] - 20.0).abs() < 1e-9 && k[0][1] == 0.0 && k[0][2].is_nan(), "{:?}", k);
        // at 27 m/s within 2.94 m/s^2: 40 m of ramp take only 1.075 m (its curve's limit): not kept; 110 m take it all
        let p2 = KeepPrm { ride_a: 2.94, ..prm };
        assert!(kept_ends(&[a.clone()], &[(68.0, 70.2)], &[(60.0, 60.0)], &[0.06], &[27.0], &p2)[0][0].is_nan());
        let k = kept_ends(&[a.clone()], &[(68.0, 70.2)], &[(130.0, 60.0)], &[0.06], &[27.0], &p2);
        assert!((k[0][0] - 27.0 * (12.0f64 / 2.94).sqrt()).abs() < 1e-9 && k[0][1] == 0.0, "{:?}", k);
        // one end kept far over the ground while the other is brought down: steeper than both brought: not kept
        let t = (vec![[0.0, 0.0], [12.0, 0.0]], vec![1443.0, 1437.0]);
        assert!(kept_ends(&[t], &[(1417.0, 1422.0)], &[(f64::NAN, 170.0)], &[0.10], &[1.0], &prm).iter().flatten().all(|v| v.is_nan()));
        // twins merging onto one abutment: neither keeps it
        let c = (vec![[0.0, 3.0], [30.0, 0.5]], vec![70.0, 71.0]);
        let d = (vec![[0.0, -3.0], [30.0, 0.0]], vec![70.0, 71.0]);
        assert!(kept_ends(&[c, d], &[(f64::NAN, 69.0), (f64::NAN, 69.0)], &[(99.0, 99.0), (99.0, 99.0)], &[0.06, 0.06], &[1.0, 1.0], &prm)
                .iter().flatten().all(|v| v.is_nan()));
    }

    #[test]
    fn an_approach_runs_to_its_first_junction() {
        // line 0 from the deck end (x = 0) east to node 7, where line 1 alone goes on (from its far end); line 1 meets
        // node 9 at x = 80, where line 2 crosses: a junction
        let l0 = RunLine { pts: vec![[0.0, 0.0], [30.0, 0.0]], nodes: vec![(0.0, 5), (30.0, 7)], stops: vec![] };
        let l1 = RunLine { pts: vec![[100.0, 0.0], [30.0, 0.0]], nodes: vec![(0.0, 11), (20.0, 9), (70.0, 7)], stops: vec![] };
        let l2 = RunLine { pts: vec![[80.0, -50.0], [80.0, 50.0]], nodes: vec![(50.0, 9)], stops: vec![] };
        let (run, pts) = approach_run(&[l0, l1, l2], 0, 1.0, true, 200.0);
        assert!((run - 79.0).abs() < 1e-9, "{}", run);
        assert!((pts[0][0] - 1.0).abs() < 1e-9 && (pts[pts.len() - 1][0] - 80.0).abs() < 1e-9, "{:?}", pts);
        assert!(pts.windows(2).all(|w| norm(sub(w[1], w[0])) <= 2.0 + 1e-9));
        // capped at max_m
        let l0 = RunLine { pts: vec![[0.0, 0.0], [300.0, 0.0]], nodes: vec![], stops: vec![] };
        let (run, pts) = approach_run(&[l0], 0, 0.0, true, 50.0);
        assert!((run - 50.0).abs() < 1e-9 && (pts[pts.len() - 1][0] - 50.0).abs() < 1e-9, "{} {:?}", run, pts.last());
        // a structure ahead ends it; walking back along a line too
        let l0 = RunLine { pts: vec![[0.0, 0.0], [300.0, 0.0]], nodes: vec![], stops: vec![40.0, 250.0] };
        assert!((approach_run(&[l0], 0, 0.0, true, 200.0).0 - 40.0).abs() < 1e-9);
        let l0 = RunLine { pts: vec![[0.0, 0.0], [300.0, 0.0]], nodes: vec![], stops: vec![40.0, 250.0] };
        assert!((approach_run(&[l0], 0, 300.0, false, 200.0).0 - 50.0).abs() < 1e-9);
        // a deck at the start of its line: the walk starts at that end, outward, onto the line going on there
        let l0 = RunLine { pts: vec![[0.0, 0.0], [30.0, 0.0]], nodes: vec![(0.0, 4), (30.0, 5)], stops: vec![30.0] };
        let l1 = RunLine { pts: vec![[-90.0, 0.0], [0.0, 0.0]], nodes: vec![(0.0, 6), (90.0, 4)], stops: vec![] };
        let (run, pts) = approach_run(&[l0, l1], 0, 0.0, false, 200.0);
        assert!((run - 90.0).abs() < 1e-9 && (pts[pts.len() - 1][0] + 90.0).abs() < 1e-9, "{} {:?}", run, pts.last());
    }
}
