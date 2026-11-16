//! Tram switches in the drawn track (plan 046, stage F): where a way's end meets two others (a switch node of the data: the
//! through track in two ways and the branch) or lies on another way's line, the branch's first metres run beside the
//! through track, and a strip of its own there would overlap the through track's (two paved strips on one another, and,
//! with the user's strips, a second pair of rails baked into the paving across the first). Here the branch's start,
//! where its centre line is nearer the through line than the strip is wide, is cut off its strip and drawn as plain
//! paving with the branch's own rails as ribbons beginning at the switch, under the through strip (one strip over it, no
//! second one); a short plate marks the frog. The rest of the branch is the strip as before. Pure.
use super::lines::{densify, offset_polyline, P2};
use crate::dm::Dm;

/// m: the ends within this of one another are one node.
pub const NODE_M: f64 = 0.5;
/// m: an end this near another way's line, farther than SIDE_END_M from its ends, is a switch on that line.
pub const SIDE_M: f64 = 0.5;
pub const SIDE_END_M: f64 = 3.0;
/// m of the branch looked at for the zone at most.
pub const ZONE_MAX_M: f64 = 30.0;
/// m: where a branch is this far from the through line it has left it.
pub fn zone_lateral(strip_w: f64) -> f64 {
    strip_w
}

/// One chain's end: (chain, end 0 first / 1 last, point, unit direction away from the end along the chain).
#[derive(Clone, Copy, Debug)]
pub struct End {
    pub chain: usize,
    pub end: usize,
    pub p: P2,
    pub d: P2,
}

fn unit(a: P2, b: P2) -> P2 {
    let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
    let l = dx.dhypot(dy).max(1e-9);
    [dx / l, dy / l]
}

pub fn ends(chains: &[Vec<P2>]) -> Vec<End> {
    let mut out = Vec::new();
    for (ci, c) in chains.iter().enumerate() {
        if c.len() < 2 {
            continue;
        }
        for e in 0..2 {
            let pts: Vec<P2> = if e == 0 { c.clone() } else { c.iter().rev().copied().collect() };
            // the direction over the first ~3 m (not the first segment: OSM's nodes lie far apart and bend)
            let mut far = pts[1];
            for q in &pts[1..] {
                far = *q;
                if (q[0] - pts[0][0]).dhypot(q[1] - pts[0][1]) >= 3.0 {
                    break;
                }
            }
            out.push(End { chain: ci, end: e, p: pts[0], d: unit(pts[0], far) });
        }
    }
    out
}

/// Distance from p to the polyline and the arc length at the nearest point.
pub fn near_line(p: P2, line: &[P2]) -> (f64, f64) {
    let (mut best, mut at, mut s) = (f64::INFINITY, 0.0, 0.0);
    for w in line.windows(2) {
        let (a, b) = (w[0], w[1]);
        let d = [b[0] - a[0], b[1] - a[1]];
        let l2 = (d[0] * d[0] + d[1] * d[1]).max(1e-12);
        let t = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
        let dist = (p[0] - (a[0] + t * d[0])).dhypot(p[1] - (a[1] + t * d[1]));
        let seg = l2.sqrt();
        if dist < best {
            best = dist;
            at = s + t * seg;
        }
        s += seg;
    }
    (best, at)
}

fn length(line: &[P2]) -> f64 {
    line.windows(2).map(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1])).sum()
}

/// A branch: its chain, which end, and the through chain it runs beside.
#[derive(Clone, Copy, Debug)]
pub struct Switch {
    pub branch: usize,
    pub end: usize,
    pub through: usize,
}

/// The switches among the chains: a node of three ends (the through pair: the two most nearly opposite; the branch: the
/// third, beside the one of the pair it turns less from), and an end alone on another chain's line.
pub fn find(chains: &[Vec<P2>]) -> Vec<Switch> {
    let es = ends(chains);
    let near = |a: P2, b: P2| (a[0] - b[0]).dhypot(a[1] - b[1]) <= NODE_M;
    let mut used = vec![false; es.len()];
    let mut out = Vec::new();
    for i in 0..es.len() {
        if used[i] {
            continue;
        }
        let group: Vec<usize> = (0..es.len()).filter(|&j| near(es[i].p, es[j].p)).collect();
        for &j in &group {
            used[j] = true;
        }
        if group.len() == 3 && group.iter().map(|&j| es[j].chain).collect::<std::collections::HashSet<_>>().len() == 3 {
            // the through pair: the two most nearly opposite
            let mut best = (f64::INFINITY, 0usize, 1usize);
            for a in 0..3 {
                for b in a + 1..3 {
                    let dot = es[group[a]].d[0] * es[group[b]].d[0] + es[group[a]].d[1] * es[group[b]].d[1];
                    if dot < best.0 {
                        best = (dot, a, b);
                    }
                }
            }
            let c = 3 - best.1 - best.2;
            let br = es[group[c]];
            let dot = |e: &End| e.d[0] * br.d[0] + e.d[1] * br.d[1];
            let (ea, eb) = (es[group[best.1]], es[group[best.2]]);
            let through = if dot(&ea) >= dot(&eb) { ea.chain } else { eb.chain };
            out.push(Switch { branch: br.chain, end: br.end, through });
        } else if group.len() == 1 {
            let e = es[i];
            let mut hit: Option<(f64, usize)> = None;
            for (ci, c) in chains.iter().enumerate() {
                if ci == e.chain || c.len() < 2 {
                    continue;
                }
                let (d, at) = near_line(e.p, c);
                if d <= SIDE_M && at > SIDE_END_M && at < length(c) - SIDE_END_M && hit.map_or(true, |h| d < h.0) {
                    hit = Some((d, ci));
                }
            }
            if let Some((_, through)) = hit {
                out.push(Switch { branch: e.chain, end: e.end, through });
            }
        }
    }
    out
}

/// Free ends: the chain ends that meet nothing (a dead end, the join to a bridge or a gap in the data), where a strip stops.
pub fn free_ends(chains: &[Vec<P2>]) -> Vec<(usize, usize)> {
    let es = ends(chains);
    let sw = find(chains);
    es.iter()
        .filter(|e| {
            !es.iter().any(|o| (o.chain, o.end) != (e.chain, e.end) && (o.p[0] - e.p[0]).dhypot(o.p[1] - e.p[1]) <= NODE_M)
                && !sw.iter().any(|s| s.branch == e.chain && s.end == e.end)
        })
        .map(|e| (e.chain, e.end))
        .collect()
}

/// The branch cut at where it has left the through line by `strip_w`: (the zone: from the switch to that point, the rest:
/// from that point on), both in the branch's own direction (end 0 first / the zone reversed for end 1 is NOT done:
/// both run away from the switch for the zone, and along the chain for the rest, as the chain was). None where the
/// zone is shorter than a metre or the branch never leaves the line within ZONE_MAX_M (a parallel one).
pub fn cut(branch: &[P2], end: usize, through: &[P2], strip_w: f64) -> Option<(Vec<P2>, Vec<P2>)> {
    let line: Vec<P2> = if end == 0 { branch.to_vec() } else { branch.iter().rev().copied().collect() };
    let line = densify(&line, 1.0);
    let mut k = None;
    for (i, p) in line.iter().enumerate() {
        let walked = (p[0] - line[0][0]).dhypot(p[1] - line[0][1]);
        if walked > ZONE_MAX_M {
            return None;
        }
        if near_line(*p, through).0 >= zone_lateral(strip_w) {
            k = Some(i);
            break;
        }
    }
    let k = k?;
    if k < 2 {
        return None;
    }
    let (zone, rest) = (line[..=k].to_vec(), line[k..].to_vec());
    if rest.len() < 2 {
        return None;
    }
    // (the rest back in the chain's own direction)
    Some((zone, if end == 0 { rest } else { rest.into_iter().rev().collect() }))
}

/// m of the branch refitted to leave the through track tangent to it (a smooth lead curve from the heel).
pub const FIT_M: f64 = 16.0;
/// The turn from the through track's direction past which a branch is not refitted (a crossing, not a turnout).
pub const FIT_MAX_TURN_COS: f64 = 0.5;

fn unit2(a: P2, b: P2) -> P2 {
    unit(a, b)
}

fn point_at(line: &[P2], arc: &[f64], s: f64) -> (P2, P2) {
    let n = line.len();
    let s = s.clamp(0.0, arc[n - 1]);
    let mut j = 0;
    while j + 2 < n && arc[j + 1] < s {
        j += 1;
    }
    let l = (arc[j + 1] - arc[j]).max(1e-9);
    let t = ((s - arc[j]) / l).clamp(0.0, 1.0);
    let d = unit2(line[j], line[j + 1]);
    ([line[j][0] + (line[j + 1][0] - line[j][0]) * t, line[j][1] + (line[j + 1][1] - line[j][1]) * t], d)
}

fn arcs(line: &[P2]) -> Vec<f64> {
    let mut a = vec![0.0; line.len()];
    for i in 1..line.len() {
        a[i] = a[i - 1] + (line[i][0] - line[i - 1][0]).dhypot(line[i][1] - line[i - 1][1]);
    }
    a
}

/// The branch (end 0 first / 1 last) refitted: it starts at the heel (the end's nearest point on the through line), leaves it
/// tangent to the through track and joins the branch's own line FIT_M on, by a cubic Hermite curve (no kink where OSM's
/// first segment points the way the node does); the rest is untouched. The line comes back in the chain's own direction.
/// Unchanged where the branch is too short, turns more than FIT_MAX_TURN_COS from the through track, or does not start at it.
pub fn refit(branch: &[P2], end: usize, through: &[P2]) -> Vec<P2> {
    let line: Vec<P2> = if end == 0 { branch.to_vec() } else { branch.iter().rev().copied().collect() };
    let line = densify(&line, 1.0);
    if line.len() < 6 || through.len() < 2 {
        return branch.to_vec();
    }
    let ta = arcs(through);
    let (d0, s0) = near_line(line[0], through);
    if d0 > SIDE_M + 1.0 {
        return branch.to_vec();
    }
    let (heel, tdir) = point_at(through, &ta, s0);
    let la = arcs(&line);
    let total = la[la.len() - 1];
    if total < 6.0 {
        return branch.to_vec();
    }
    let fit = FIT_M.min(0.6 * total);
    let (p1, d1) = point_at(&line, &la, fit);
    let (_, ahead) = point_at(&line, &la, 4.0f64.min(total));
    // the through direction pointing the way the branch goes
    let mut t0 = tdir;
    if t0[0] * (ahead[0]) + t0[1] * ahead[1] < 0.0 {
        t0 = [-t0[0], -t0[1]];
    }
    if t0[0] * d1[0] + t0[1] * d1[1] < FIT_MAX_TURN_COS {
        return branch.to_vec();
    }
    let chord = (p1[0] - heel[0]).dhypot(p1[1] - heel[1]);
    let n = chord.ceil().max(2.0) as usize;
    let mut out: Vec<P2> = Vec::with_capacity(line.len());
    for i in 0..=n {
        let u = i as f64 / n as f64;
        let (u2, u3) = (u * u, u * u * u);
        let (h00, h10, h01, h11) = (2.0 * u3 - 3.0 * u2 + 1.0, u3 - 2.0 * u2 + u, -2.0 * u3 + 3.0 * u2, u3 - u2);
        out.push([
            h00 * heel[0] + h10 * chord * t0[0] + h01 * p1[0] + h11 * chord * d1[0],
            h00 * heel[1] + h10 * chord * t0[1] + h01 * p1[1] + h11 * chord * d1[1],
        ]);
    }
    for (i, p) in line.iter().enumerate() {
        if la[i] > fit + 1e-6 {
            out.push(*p);
        }
    }
    if end == 0 { out } else { out.into_iter().rev().collect() }
}

/// The signed distance along direction `dir` (unit) from `p` to the nearest crossing of the ray p + t dir (t in
/// [-reach, reach]) with the polyline, 0 where none: how far a sleeper across the through track must reach to the branch.
pub fn ray_reach(p: P2, dir: P2, line: &[P2], reach: f64) -> f64 {
    let mut best = 0.0f64;
    let mut best_abs = f64::INFINITY;
    for w in line.windows(2) {
        let (a, b) = (w[0], w[1]);
        let e = [b[0] - a[0], b[1] - a[1]];
        let den = dir[0] * e[1] - dir[1] * e[0];
        if den.abs() < 1e-12 {
            continue;
        }
        let ap = [a[0] - p[0], a[1] - p[1]];
        let t = (ap[0] * e[1] - ap[1] * e[0]) / den;
        let u = (ap[0] * dir[1] - ap[1] * dir[0]) / den;
        if (0.0..=1.0).contains(&u) && t.abs() <= reach && t.abs() < best_abs {
            best = t;
            best_abs = t.abs();
        }
    }
    best
}

/// The first crossing of two polylines a and b: (point, arc on a, arc on b), None where they do not cross.
pub fn crossing(a: &[P2], b: &[P2]) -> Option<(P2, f64, f64)> {
    let (aa, ab) = (arcs(a), arcs(b));
    let mut best: Option<(P2, f64, f64)> = None;
    for i in 0..a.len().saturating_sub(1) {
        for j in 0..b.len().saturating_sub(1) {
            let (p, q) = (a[i], b[j]);
            let (e, f) = ([a[i + 1][0] - p[0], a[i + 1][1] - p[1]], [b[j + 1][0] - q[0], b[j + 1][1] - q[1]]);
            let den = e[0] * f[1] - e[1] * f[0];
            if den.abs() < 1e-12 {
                continue;
            }
            let pq = [q[0] - p[0], q[1] - p[1]];
            let t = (pq[0] * f[1] - pq[1] * f[0]) / den;
            let u = (pq[0] * e[1] - pq[1] * e[0]) / den;
            if (0.0..=1.0).contains(&t) && (0.0..=1.0).contains(&u) {
                let sa = aa[i] + t * (aa[i + 1] - aa[i]);
                if best.map_or(true, |bb| sa < bb.1) {
                    best = Some(([p[0] + e[0] * t, p[1] + e[1] * t], sa, ab[j] + u * (ab[j + 1] - ab[j])));
                }
            }
        }
    }
    best
}

/// A turnout's geometry for the 3D track (the branch's line oriented from the heel): the arc where its axis has left the
/// through axis by a sleeper's reach (ZONE_LATERAL_M: no sleeper of its own before it), the arc where the rails have
/// parted (DIVERGE_M), and the frog (the branch's inner rail crossing the through track's rail on the branch's side): the
/// point and its arcs on the branch's axis and the through axis.
pub struct Turnout {
    pub zone_len: f64,
    pub div_len: f64,
    pub frog: Option<(P2, f64, f64)>,
    /// the side of the through track the branch is on (+1 left)
    pub side: f64,
}

/// m the branch's axis is from the through axis where a sleeper of its own may start.
pub const ZONE_LATERAL_M: f64 = 2.6;
/// m the branch's axis is from the through axis where its rails have parted from the stock rails.
pub const DIVERGE_M: f64 = 0.16;

/// The turnout of `branch` (oriented from the heel) off `through` for gauge g (the rails' centres g + head apart).
pub fn turnout(branch: &[P2], through: &[P2], g: f64, head: f64) -> Turnout {
    let la = arcs(branch);
    let (mut zone, mut div) = (la[la.len() - 1], la[la.len() - 1]);
    let (mut zone_set, mut div_set) = (false, false);
    for (i, p) in branch.iter().enumerate() {
        let d = near_line(*p, through).0;
        if !div_set && d >= DIVERGE_M {
            div = la[i];
            div_set = true;
        }
        if !zone_set && d >= ZONE_LATERAL_M {
            zone = la[i];
            zone_set = true;
        }
    }
    // the side of the through track the branch is on: from a point well out
    let probe = branch[branch.len().min(8) - 1];
    let ta = arcs(through);
    let (_, s) = near_line(probe, through);
    let (q, td) = point_at(through, &ta, s);
    let side = if td[0] * (probe[1] - q[1]) - td[1] * (probe[0] - q[0]) >= 0.0 { 1.0 } else { -1.0 };
    let half = g / 2.0 + head / 2.0;
    // the branch's inner rail (toward the through track) and the through track's rail on the branch's side
    let (bd, bt) = (unit2(branch[0], branch[branch.len().min(8) - 1]), 0.0);
    let _ = bt;
    let bside = if bd[0] * (q[1] - probe[1]) - bd[1] * (q[0] - probe[0]) >= 0.0 { 1.0 } else { -1.0 };
    let inner = offset_polyline(branch, bside * half);
    let stock = offset_polyline(through, side * half);
    Turnout { zone_len: zone, div_len: div, frog: crossing(&inner, &stock), side }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn straight(x0: f64, x1: f64, y: f64) -> Vec<P2> {
        let n = ((x1 - x0).abs() / 2.0) as usize + 1;
        (0..=n).map(|i| [x0 + (x1 - x0) * i as f64 / n as f64, y]).collect()
    }

    /// a through track along y = 0 in two ways meeting at x = 50, a branch leaving at 10 degrees
    fn node() -> Vec<Vec<P2>> {
        let a = straight(0.0, 50.0, 0.0);
        let b = straight(50.0, 100.0, 0.0);
        let br: Vec<P2> = (0..=30).map(|i| [50.0 + 2.0 * i as f64 * 0.985, 2.0 * i as f64 * 0.174]).collect();
        vec![a, b, br]
    }

    #[test]
    fn a_node_of_three_ends_is_a_switch_and_the_branch_runs_beside_the_nearer_through_way() {
        let sw = find(&node());
        assert_eq!(sw.len(), 1);
        assert_eq!((sw[0].branch, sw[0].end, sw[0].through), (2, 0, 1), "the branch leaves along way 1");
    }

    #[test]
    fn an_end_on_another_ways_line_is_a_switch_and_a_plain_joint_is_not() {
        let main = straight(0.0, 100.0, 0.0);
        let side: Vec<P2> = (0..=20).map(|i| [50.0 + i as f64, 0.1 + i as f64 * 0.4]).collect();
        let sw = find(&[main.clone(), side]);
        assert_eq!(sw.len(), 1);
        assert_eq!((sw[0].branch, sw[0].through), (1, 0));
        let next = straight(100.0, 150.0, 0.0);
        assert!(find(&[main, next]).is_empty(), "two ways end to end: no switch");
    }

    #[test]
    fn the_branch_is_cut_where_it_has_left_the_strip() {
        let ch = node();
        let (zone, rest) = cut(&ch[2], 0, &ch[1], 2.4).unwrap();
        let last = zone[zone.len() - 1];
        assert!(near_line(last, &ch[1]).0 >= 2.4 && near_line(zone[zone.len() - 2], &ch[1]).0 < 2.4);
        assert_eq!(rest[0], last, "the rest starts where the zone ends");
        // the other end: the same branch stored the other way round
        let rev: Vec<P2> = ch[2].iter().rev().copied().collect();
        let (z2, r2) = cut(&rev, 1, &ch[1], 2.4).unwrap();
        assert_eq!(z2.len(), zone.len());
        assert_eq!(r2[r2.len() - 1], last, "the rest ends where the zone ends (the chain's own direction)");
        // a parallel track never leaves it
        let par = straight(50.0, 100.0, 1.5);
        assert!(cut(&par, 0, &ch[1], 2.4).is_none());
    }

    #[test]
    fn free_ends_are_the_ones_that_meet_nothing() {
        let f = free_ends(&node());
        assert!(f.contains(&(0, 0)) && f.contains(&(1, 1)) && f.contains(&(2, 1)));
        assert_eq!(f.len(), 3, "the node's three ends are not free");
    }

    #[test]
    fn a_refitted_branch_leaves_the_heel_tangent_to_the_through_track_and_joins_its_own_line() {
        let through = straight(0.0, 100.0, 0.0);
        // OSM's branch: starts on the line at (50, 0), its first segment pointing 20 degrees off, then on at 8 degrees
        let mut br: Vec<P2> = vec![[50.0, 0.0], [60.0, 3.64]];
        for i in 1..=12 {
            br.push([60.0 + i as f64 * 4.0 * 0.99, 3.64 + i as f64 * 4.0 * 0.139]);
        }
        let fit = refit(&br, 0, &through);
        assert!((fit[0][1]).abs() < 1e-9 && (fit[0][0] - 50.0).abs() < 1e-6, "starts at the heel: {:?}", fit[0]);
        let d = unit(fit[0], fit[1]);
        assert!(d[1].abs() < 0.06, "tangent to the through track (the first metre's chord): {:?}", d);
        let (last_in, last_out) = (br[br.len() - 1], fit[fit.len() - 1]);
        assert!((last_in[0] - last_out[0]).abs() < 1e-6 && (last_in[1] - last_out[1]).abs() < 1e-6, "the tail is OSM's");
        // no kink: the direction changes by less than 8 degrees between neighbouring metres
        for w in fit.windows(3) {
            let (a, b) = (unit(w[0], w[1]), unit(w[1], w[2]));
            assert!(a[0] * b[0] + a[1] * b[1] > 0.99, "{:?} {:?}", w, (a, b));
        }
        // the same end stored the other way round
        let rev: Vec<P2> = br.iter().rev().copied().collect();
        let fit2 = refit(&rev, 1, &through);
        assert_eq!(fit2.len(), fit.len());
        assert!((fit2[fit2.len() - 1][0] - fit[0][0]).abs() < 1e-9);
    }

    #[test]
    fn a_turnouts_frog_is_where_the_inner_rail_crosses_the_stock_rail() {
        let through = straight(0.0, 100.0, 0.0);
        let br: Vec<P2> = (0..=40).map(|i| [50.0 + i as f64 * 0.995, 0.004 * (i * i) as f64]).collect();
        let br = refit(&br, 0, &through);
        let t = turnout(&br, &through, 1.435, 0.075);
        let (p, sb, st) = t.frog.expect("the inner rail crosses the stock rail");
        // on both rails within 5 mm
        let half = 1.435 / 2.0 + 0.0375;
        let inner = offset_polyline(&br, -t.side * half);
        let stock = offset_polyline(&through, t.side * half);
        assert!(near_line(p, &inner).0 < 0.005 && near_line(p, &stock).0 < 0.005, "{p:?}");
        assert!(sb > 0.0 && st > 40.0);
        assert!(t.div_len < t.zone_len, "the rails part before the sleepers may");
    }

    #[test]
    fn a_sleeper_across_the_through_track_reaches_the_branch() {
        let br: Vec<P2> = (0..=20).map(|i| [50.0 + i as f64, 0.5 + 0.1 * i as f64]).collect();
        let r = ray_reach([55.0, 0.0], [0.0, 1.0], &br, 8.0);
        assert!((r - 1.0).abs() < 0.01, "{r}");
        assert_eq!(ray_reach([55.0, -20.0], [0.0, 1.0], &br, 8.0), 0.0);
    }
}
