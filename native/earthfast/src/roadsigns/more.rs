//! The rules of the second round (task 017, stage 2b): narrowing, chevron boards, side roads, give way and stop, tunnels,
//! pedestrian zones and cycle paths, bus stops and car parks. Same principle as the first round: doubt, no sign.
use super::*;

const CHEVRON_N: usize = 3;             // boards across one bend
const NARROW_MIN_DIFF_M: f64 = 0.75;    // half widths: a road narrowing by less is the same road to the eye
const THROUGH_COS: f64 = 0.85;          // a road leaving this straight on is the one the traffic goes on along
const BUS_ROAD_M: f64 = 15.0;           // a bus stop belongs to the road this near at most
const PARK_ROAD_M: f64 = 20.0;
const PORTAL_ON_M: f64 = 4.0;
const PORTAL_BACK_M: f64 = 10.0;        // the tunnel sign this far before the portal
const PRIORITY_BACK_M: f64 = 1.5;
const PRIORITY_ON_M: f64 = 2.5;
const PATH_IN_M: f64 = 6.0;
const PATH_MIN_M: f64 = 40.0;
const SIDE_MIN_RANK: i64 = SECONDARY;   // the main road of a side-road warning
const SIDE_ROAD_MIN_RANK: i64 = 2;      // the side road: unclassified and up, a drive is not announced

/// The nearest road to q within `max` m (not a motorway, ramp or ring): (road, arc length, distance, whether q lies to the
/// left of the geometry's direction there).
fn nearest(net: &Net, q: [f64; 2], max: f64) -> Option<(usize, f64, f64, bool)> {
    let mut best: Option<(usize, f64, f64, bool)> = None;
    for (r, rd) in net.roads.iter().enumerate() {
        if rd.rank < 1 || rd.rank > TRUNK || rd.link || rd.ring { continue; }
        for k in rd.a..rd.b.saturating_sub(1) {
            let (ax, ay, bx, by) = (net.pts[[k, 0]], net.pts[[k, 1]], net.pts[[k + 1, 0]], net.pts[[k + 1, 1]]);
            let (dx, dy) = (bx - ax, by - ay);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((q[0] - ax) * dx + (q[1] - ay) * dy) / l2).clamp(0.0, 1.0);
            let d = ((ax + t * dx - q[0]).powi(2) + (ay + t * dy - q[1]).powi(2)).sqrt();
            if d <= max && best.map_or(true, |b| d < b.2) {
                best = Some((r, rd.s[k - rd.a] + t * l2.sqrt(), d, dx * (q[1] - ay) - dy * (q[0] - ax) > 0.0));
            }
        }
    }
    best
}

/// Give way and stop: the map's `give_way` / `stop` nodes (pois kinds 0 and 1) with a stated direction (a: +1 along the
/// way's geometry, -1 against it; 0 stated nowhere: only a one-way road says it itself).
fn priority(net: &Net, a: &Input, out: &mut Vec<Cand>) {
    for k in 0..a.pois.nrows() {
        let kind = a.pois[[k, 2]] as i64;
        if kind != POI_GIVE_WAY && kind != POI_STOP { continue; }
        let q = [a.pois[[k, 0]], a.pois[[k, 1]]];
        // the minor road of the junction the node may lie on: the lowest rank within PRIORITY_ON_M, then the nearest
        let mut pick: Option<(usize, f64, i64, f64)> = None;
        for (r, rd) in net.roads.iter().enumerate() {
            if rd.rank < 1 || rd.rank > TRUNK || rd.ring { continue; }
            for k2 in rd.a..rd.b.saturating_sub(1) {
                let (ax, ay, bx, by) = (net.pts[[k2, 0]], net.pts[[k2, 1]], net.pts[[k2 + 1, 0]], net.pts[[k2 + 1, 1]]);
                let (dx, dy) = (bx - ax, by - ay);
                let l2 = (dx * dx + dy * dy).max(1e-12);
                let t = (((q[0] - ax) * dx + (q[1] - ay) * dy) / l2).clamp(0.0, 1.0);
                let d = ((ax + t * dx - q[0]).powi(2) + (ay + t * dy - q[1]).powi(2)).sqrt();
                if d > PRIORITY_ON_M { continue; }
                let s = rd.s[k2 - rd.a] + t * l2.sqrt();
                if pick.map_or(true, |b| (rd.rank, d) < (b.2, b.3)) { pick = Some((r, s, rd.rank, d)); }
            }
        }
        let Some((r, s, _, _)) = pick else { continue };
        let rd = &net.roads[r];
        let stated = a.pois[[k, 3]];
        let fwd = if stated > 0.0 { true } else if stated < 0.0 { false } else if rd.dir != 0 { rd.dir > 0 } else { continue };
        if (rd.dir > 0 && !fwd) || (rd.dir < 0 && fwd) { continue; }
        let total = rd.s[rd.b - rd.a - 1];
        let at = if fwd { s - PRIORITY_BACK_M } else { s + PRIORITY_BACK_M };
        if at <= 0.0 || at >= total { continue; }
        out.push(cand(net, if kind == POI_STOP { STOP } else { GIVE_WAY }, r, at, fwd, true));
    }
}

/// Tunnels: the portals (pois kind 4: x, y, travel x, travel y into the tunnel); a sign PORTAL_BACK_M before each.
fn tunnels(net: &Net, a: &Input, out: &mut Vec<Cand>) {
    for k in 0..a.pois.nrows() {
        if a.pois[[k, 2]] as i64 != POI_TUNNEL { continue; }
        let Some((r, s, _, _)) = nearest(net, [a.pois[[k, 0]], a.pois[[k, 1]]], PORTAL_ON_M) else { continue };
        let rd = &net.roads[r];
        let total = rd.s[rd.b - rd.a - 1];
        let t = net.chord(r, (s - 3.0).max(0.0), (s + 3.0).min(total), true);
        let fwd = dot(t, [a.pois[[k, 3]], a.pois[[k, 4]]]) > 0.0;
        if (rd.dir > 0 && !fwd) || (rd.dir < 0 && fwd) { continue; }
        let at = if fwd { s - PORTAL_BACK_M } else { s + PORTAL_BACK_M };
        if at <= 0.0 || at >= total { continue; }
        out.push(cand(net, TUNNEL, r, at, fwd, false));
    }
}

/// Bus stops (kind 2) and car park entrances (kind 3): a sign at the stop for the traffic on its side of the road.
fn stops(net: &Net, a: &Input, out: &mut Vec<Cand>) {
    for k in 0..a.pois.nrows() {
        let kind = a.pois[[k, 2]] as i64;
        let (rule, max) = match kind { POI_BUS_STOP => (BUS_STOP, BUS_ROAD_M), POI_PARKING => (PARKING, PARK_ROAD_M), _ => continue };
        let Some((r, s, _, left)) = nearest(net, [a.pois[[k, 0]], a.pois[[k, 1]]], max) else { continue };
        let rd = &net.roads[r];
        if rd.living { continue; }
        let fwd = if rd.dir != 0 { rd.dir > 0 } else if a.left_hand { left } else { !left };
        let total = rd.s[rd.b - rd.a - 1];
        if s <= 1.0 || s >= total - 1.0 { continue; }
        out.push(cand(net, rule, r, s, fwd, false));
    }
}

/// A road narrowing: where a road of the rank goes on, straight, as one of the same rank that is narrower (half width at least
/// NARROW_MIN_DIFF_M less and under p[10] of the wider), out of town. One sign before it, both sides.
fn narrowing(net: &Net, ends: &HashMap<i64, usize>, outgoing: &HashMap<i64, Vec<usize>>, p: &[f64; N_PARAMS],
             out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.urban || rd.link || rd.ring || rd.rank < TERTIARY || rd.rank > TRUNK { continue; }
        let len = net.len(e);
        if len < MIN_PIECE_M || *ends.get(&ed.to).unwrap_or(&0) != 2 { continue; }
        let d = net.end_dir(e);
        let narrower = outgoing.get(&ed.to).unwrap_or(&none).iter().any(|&o| {
            let r2 = &net.roads[net.edges[o].road];
            net.edges[o].road != ed.road && r2.rank == rd.rank && !r2.link && !r2.ring && !r2.urban
                && rd.hw - r2.hw >= NARROW_MIN_DIFF_M && r2.hw <= p[10] * rd.hw && dot(d, net.start_dir(o)) > THROUGH_COS
        });
        if narrower { before(net, e, len, p, out, NARROW); }
    }
}

/// Chevron boards on the outside of a sharp bend (p[8] degrees within p[9] m), CHEVRON_N across it, pointing the way it turns.
fn chevrons(net: &Net, p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.urban || rd.link || rd.ring || rd.rank < p[3] as i64 || rd.rank > TRUNK { continue; }
        let len = net.len(e);
        if len < MIN_PIECE_M { continue; }
        for b in bends(net, e, p[9], p[8].to_radians()).into_iter().filter(|b| b.from >= BEND_EDGE_M && b.to <= len - BEND_EDGE_M) {
            let (rule, outer) = if b.turn > 0.0 { (CHEVRON_LEFT, 1.0) } else { (CHEVRON_RIGHT, -1.0) };
            for i in 0..CHEVRON_N {
                let u = b.from + (b.to - b.from) * (i as f64 + 1.0) / (CHEVRON_N as f64 + 1.0);
                let mut c = cand(net, rule, ed.road, arc_at(net, e, u), ed.fwd, false);
                c.side = Some(outer);      // (+1 the right of the travel: the outside of a turn to the left)
                out.push(c);
            }
        }
    }
}

/// Side roads: on a main road a junction of three where the road goes straight on and a lower road joins from one side, out of
/// town; the warning before it on the side the road joins from.
fn side_roads(net: &Net, ends: &HashMap<i64, usize>, incoming: &HashMap<i64, Vec<usize>>,
              outgoing: &HashMap<i64, Vec<usize>>, p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.urban || rd.link || rd.ring || rd.rank < SIDE_MIN_RANK || rd.rank > TRUNK { continue; }
        let len = net.len(e);
        if len < MIN_PIECE_M || *ends.get(&ed.to).unwrap_or(&0) != 3 { continue; }
        let d = net.end_dir(e);
        // the other two pieces at the node: (road, piece, direction leaving the node)
        let mut others: Vec<(usize, usize, [f64; 2])> = Vec::new();
        let mut add = |road: usize, piece: usize, dir: [f64; 2]| {
            if (road, piece) != (ed.road, ed.piece) && !others.iter().any(|o| (o.0, o.1) == (road, piece)) { others.push((road, piece, dir)); }
        };
        for &o in outgoing.get(&ed.to).unwrap_or(&none) { add(net.edges[o].road, net.edges[o].piece, net.start_dir(o)); }
        for &i in incoming.get(&ed.to).unwrap_or(&none) {
            let v = net.end_dir(i);
            add(net.edges[i].road, net.edges[i].piece, [-v[0], -v[1]]);
        }
        if others.len() != 2 { continue; }
        let through = if dot(others[0].2, d) > THROUGH_COS { 0 } else if dot(others[1].2, d) > THROUGH_COS { 1 } else { continue };
        let (sroad, _, sdir) = others[1 - through];
        let sr = &net.roads[sroad];
        if sr.rank < SIDE_ROAD_MIN_RANK || sr.rank >= rd.rank || sr.link || sr.ring || sr.living { continue; }
        if net.roads[others[through].0].rank < SIDE_MIN_RANK { continue; }
        let left = d[0] * sdir[1] - d[1] * sdir[0] > 0.0;
        before(net, e, len, p, out, if left { SIDE_LEFT } else { SIDE_RIGHT });
    }
}

/// Pedestrian zones (entering from an ordinary road) and cycle paths (a road ending in one that goes straight on).
fn paths(net: &Net, incoming: &HashMap<i64, Vec<usize>>, out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    let plain = |i: usize| { let r = &net.roads[net.edges[i].road]; !r.living && !r.foot && !r.cycle && !r.link && !r.ring && r.rank >= 1 };
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if !(rd.foot || rd.cycle) { continue; }
        let len = net.len(e);
        if len < PATH_MIN_M { continue; }
        let d = net.start_dir(e);
        let inn = incoming.get(&ed.from).unwrap_or(&none);
        if rd.foot && inn.iter().any(|&i| plain(i)) {
            out.push(cand(net, PED_ZONE, ed.road, arc_at(net, e, PATH_IN_M.min(len * 0.5)), ed.fwd, false));
        }
        if rd.cycle && inn.iter().any(|&i| plain(i) && dot(net.end_dir(i), d) > THROUGH_COS) {
            out.push(cand(net, BICYCLE, ed.road, arc_at(net, e, PATH_IN_M.min(len * 0.5)), ed.fwd, false));
        }
    }
}

/// Every rule of the round.
pub(super) fn all(net: &Net, ends: &HashMap<i64, usize>, incoming: &HashMap<i64, Vec<usize>>,
                  outgoing: &HashMap<i64, Vec<usize>>, a: &Input, out: &mut Vec<Cand>) {
    narrowing(net, ends, outgoing, &a.params, out);
    chevrons(net, &a.params, out);
    side_roads(net, ends, incoming, outgoing, &a.params, out);
    priority(net, a, out);
    tunnels(net, a, out);
    paths(net, incoming, out);
    stops(net, a, out);
}
