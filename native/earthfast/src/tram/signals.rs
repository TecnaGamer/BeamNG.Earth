//! Tram signals (plan 046 stage H, BNE_TRAM_SIGNALS): where a ground tram track crosses a carriageway at a signalled
//! junction (an OSM highway=traffic_signals node within NEAR_M of the crossing) or passes an OSM railway=signal node,
//! a signal stands before the stop line, facing the approaching tram, on its own post beside the track, at the kerb
//! side of the traffic (right of the tram's travel in right-hand traffic, left in left-hand traffic).
//!
//! One crossing gives a signal per direction of travel; a second track of a pair keeps the outer ones only (a
//! candidate whose sweep reaches another track is dropped, so the pair's inner sides stay free). The signal's working
//! cycle is a rule of the junction, not of the tile: the cycle length, green time and offset are hashed from the
//! smallest OSM signal node round the crossing, so every tile, client and rebuild agrees (the mod's tramsignals.lua
//! runs the phases; with the car signals of the navgraph at the same junction it takes their sequence instead).
//! Pure given its inputs; the heights are the caller's.
use crate::dm::Dm;

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// m: a crossing is signalled when a mapped traffic signal node is this near it.
pub const NEAR_M: f64 = 30.0;
/// m: a railway=signal node this near a crossing is that crossing's own.
pub const RAIL_SIGNAL_NEAR_M: f64 = 15.0;
/// m: the nodes of one junction (its arms' signals) are the ones within this of the crossing; the smallest is its key.
pub const CLUSTER_M: f64 = 45.0;
/// A skew crossing's stop line is hw / sin before it; a crossing flatter than this is no crossing (railxing.MIN_SIN).
pub const MIN_SIN: f64 = 0.35;
/// m before the carriageway's edge (along the track): the stop line, a crosswalk's width and the post's margin.
pub const BEFORE_M: f64 = 3.5;
/// m: the least a post stands out from the track's axis (a tram is 2.4 m wide: its side + 0.8 m).
pub const LATERAL_M: f64 = 2.0;
/// m: how far out the post is looked for (to clear a carriageway the track lies in).
pub const LATERAL_MAX_M: f64 = 9.0;
pub const LATERAL_STEP_M: f64 = 0.5;
/// m a post keeps outside a carriageway's drawn edge.
pub const ROAD_OFF_M: f64 = 0.9;
/// m a post keeps off any tram track's axis.
pub const TRACK_CLEAR_M: f64 = 1.9;
/// m: another track this near on the post's side makes the two a pair (a median between them, a street's two tracks): the
/// direction whose kerb side is towards the other track has no post there, its own stands on the outer side.
pub const PAIR_M: f64 = 12.0;
/// m: two posts nearer than this are one (the first in the sorted order stays).
pub const MERGE_M: f64 = 1.0;
/// m ahead of the stop line along the track a diverging branch is looked for (the junction's switch).
pub const AHEAD_M: f64 = 70.0;
/// m: another part's end this near the approaching track is on it (a switch).
pub const BRANCH_ON_M: f64 = 1.2;
/// sine of the least angle a branch diverges at (8 degrees); less is the track's own continuation.
pub const BRANCH_SIN: f64 = 0.14;
const SAMPLE_M: f64 = 2.0;
const CYCLE_MIN_S: f64 = 60.0;
const CYCLE_STEP_S: f64 = 5.0;
const CYCLE_STEPS: u64 = 7; // 60, 65 .. 90 s
const GREEN_SHARE: f64 = 0.2;

/// The signal faces: bars of white lamps (the ex-USSR, Czechia and its neighbours, the Americas) ...
pub const STYLE_BAR: u8 = 0;
/// ... or the BOStrab-like F0 / F1 head (Germany, Austria, Switzerland, the Netherlands, Finland and the rest).
pub const STYLE_BOSTRAB: u8 = 1;

const BAR_COUNTRIES: &[&str] = &[
    "AM", "AZ", "BY", "EE", "GE", "KG", "KZ", "LT", "LV", "MD", "RU", "TJ", "TM", "UA", "UZ", // the ex-USSR (railxing.SU)
    "CZ", "SK", "PL", "HU", "RO", "BG", "RS", "HR", "SI", "BA", "MK", "AL", // its neighbours' bar signals
    "US", "CA", "MX", "BR", "AR", "CL", "CO", "PE", "VE", "UY", "PY", "BO", "EC", // the Americas
];

/// The style of the signal faces in a country (ISO alpha-2; empty or unknown: BOStrab).
pub fn style_of(country: &str) -> u8 {
    let c = country.trim().to_ascii_uppercase();
    if BAR_COUNTRIES.contains(&c.as_str()) { STYLE_BAR } else { STYLE_BOSTRAB }
}

/// One signal: the post's place, its yaw (degrees, counter-clockwise from +x, of the model's +x axis = the right of
/// the tram's travel: the face is towards the model's -y), the face style, the cycle, and the junction's key node.
#[derive(Clone, Debug, PartialEq)]
pub struct Signal {
    pub x: f64,
    pub y: f64,
    pub yaw: f64,
    pub style: u8,
    pub cycle: f64,
    pub offset: f64,
    pub green: f64,
    pub key: [f64; 2],
    /// 0: a traffic signals junction, 1: an OSM railway=signal node
    pub rule: u8,
    /// the side a track branches off to ahead of the stop line within AHEAD_M (a switch): 1 left of the travel, -1 right, 0 none
    pub branch: i8,
}

pub type Pts = Vec<[f64; 2]>;

fn splitmix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// The junction's hash: its key node (centimetres) and a salt, a 64-bit integer.
fn hash(key: [f64; 2], salt: u64) -> u64 {
    let a = (key[0] * 100.0).round() as i64 as u64;
    let b = (key[1] * 100.0).round() as i64 as u64;
    splitmix(splitmix(a ^ salt.wrapping_mul(0xD6E8_FEB8_6659_FD93)).wrapping_add(b))
}

/// (cycle, offset, green) in seconds of the signal of a junction keyed by node `key`: the cycle 60 .. 90 s in 5 s steps,
/// the green a fifth of it, the offset a whole second in the cycle. The same for every signal of the junction.
pub fn cycle_of(key: [f64; 2], rule: u8) -> (f64, f64, f64) {
    let salt = 0x7A11 + rule as u64;
    let cycle = CYCLE_MIN_S + CYCLE_STEP_S * (hash(key, salt) % CYCLE_STEPS) as f64;
    let offset = (hash(key, salt + 100) % cycle as u64) as f64;
    let green = (GREEN_SHARE * cycle).round();
    (cycle, offset, green)
}

fn dist_seg(p: [f64; 2], a: [f64; 2], b: [f64; 2]) -> f64 {
    let (ux, uy) = (b[0] - a[0], b[1] - a[1]);
    let l2 = ux * ux + uy * uy;
    let t = if l2 < 1e-12 { 0.0 } else { (((p[0] - a[0]) * ux + (p[1] - a[1]) * uy) / l2).clamp(0.0, 1.0) };
    (p[0] - a[0] - t * ux).dhypot(p[1] - a[1] - t * uy)
}

fn dist_line(p: [f64; 2], line: &[[f64; 2]]) -> f64 {
    let mut best = f64::INFINITY;
    for w in line.windows(2) {
        best = best.min(dist_seg(p, w[0], w[1]));
    }
    best
}

fn bbox(line: &[[f64; 2]]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for p in line {
        b[0] = b[0].min(p[0]);
        b[1] = b[1].min(p[1]);
        b[2] = b[2].max(p[0]);
        b[3] = b[3].max(p[1]);
    }
    b
}

fn overlap(a: [f64; 4], b: [f64; 4]) -> bool {
    a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1]
}

/// (distance along segment a, the crossing point, the sine of the angle) where segments a0-a1 and b0-b1 cross; None
/// when they do not (a touch at a's far end is the next segment's: a is half-open, b closed).
fn cross(a0: [f64; 2], a1: [f64; 2], b0: [f64; 2], b1: [f64; 2]) -> Option<(f64, [f64; 2], f64)> {
    let (dx, dy) = (a1[0] - a0[0], a1[1] - a0[1]);
    let (ex, ey) = (b1[0] - b0[0], b1[1] - b0[1]);
    let den = dx * ey - dy * ex;
    let la = dx.dhypot(dy);
    let lb = ex.dhypot(ey);
    if la < 1e-9 || lb < 1e-9 || den.abs() < 1e-12 {
        return None;
    }
    let (fx, fy) = (b0[0] - a0[0], b0[1] - a0[1]);
    let t = (fx * ey - fy * ex) / den;
    let u = (fx * dy - fy * dx) / den;
    if !(0.0..1.0).contains(&t) || !(0.0..=1.0).contains(&u) {
        return None;
    }
    Some((t * la, [a0[0] + t * dx, a0[1] + t * dy], (den / (la * lb)).abs()))
}

fn arcs(line: &[[f64; 2]]) -> Vec<f64> {
    let mut out = Vec::with_capacity(line.len());
    let mut s = 0.0;
    out.push(0.0);
    for w in line.windows(2) {
        s += (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1]);
        out.push(s);
    }
    out
}

/// The point and unit tangent of `line` at arc length s (arc: its arc lengths); None outside 0 ..= total.
fn at_arc(line: &[[f64; 2]], arc: &[f64], s: f64) -> Option<([f64; 2], [f64; 2])> {
    let total = *arc.last()?;
    if !(0.0..=total).contains(&s) || line.len() < 2 {
        return None;
    }
    let j = arc.partition_point(|&v| v < s).clamp(1, line.len() - 1) - 1;
    let (a, b) = (line[j], line[j + 1]);
    let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
    if l < 1e-9 {
        return None;
    }
    let t = ((s - arc[j]) / l).clamp(0.0, 1.0);
    Some(([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])], [(b[0] - a[0]) / l, (b[1] - a[1]) / l]))
}

/// The side a track diverges to ahead of a signal: the approaching track `part` from arc s_sig in direction sgn (+1 / -1 along
/// it), AHEAD_M of it. Another part whose end lies on it (a T: its end on the track's inside, or a Y: both ends at the track's
/// end, a straight continuation and a diverging branch) and leaves forward at 8 to 72 degrees is a branch: 1 left, -1 right,
/// the nearest one's, 0 when none. Pure.
pub fn branch_side(parts: &[Pts], pi: usize, arc: &[f64], s_sig: f64, sgn: f64) -> i8 {
    let part = &parts[pi];
    let mut line: Vec<[f64; 2]> = Vec::new();
    let mut k = 0.0;
    let mut terminal = false;
    while k <= AHEAD_M + 1e-9 {
        match at_arc(part, arc, s_sig + sgn * k) {
            Some((p, _)) => line.push(p),
            None => {
                terminal = true;
                // the part's own end is the line's last point
                let end = if sgn > 0.0 { part[part.len() - 1] } else { part[0] };
                if line.last().map_or(true, |l| (l[0] - end[0]).dhypot(l[1] - end[1]) > 0.05) {
                    line.push(end);
                }
                break;
            }
        }
        k += SAMPLE_M;
    }
    if line.len() < 2 {
        return 0;
    }
    let la = arcs(&line);
    let line_len = *la.last().unwrap();
    let mut diverging: Vec<(f64, i8, bool)> = Vec::new(); // (position, side, at the terminal end)
    let mut straight_at_end = false;
    for (oi, o) in parts.iter().enumerate() {
        if oi == pi || o.len() < 2 {
            continue;
        }
        let ends = [(o[0], o[1]), (o[o.len() - 1], o[o.len() - 2])];
        for (e, nxt) in ends {
            let (mut best_d, mut best_pos, mut best_t) = (f64::INFINITY, 0.0, [1.0, 0.0]);
            for (j, w) in line.windows(2).enumerate() {
                let (ux, uy) = (w[1][0] - w[0][0], w[1][1] - w[0][1]);
                let l2 = ux * ux + uy * uy;
                if l2 < 1e-12 {
                    continue;
                }
                let t = (((e[0] - w[0][0]) * ux + (e[1] - w[0][1]) * uy) / l2).clamp(0.0, 1.0);
                let d = (e[0] - w[0][0] - t * ux).dhypot(e[1] - w[0][1] - t * uy);
                if d < best_d {
                    let l = l2.sqrt();
                    best_d = d;
                    best_pos = la[j] + t * l;
                    best_t = [ux / l, uy / l];
                }
            }
            if best_d > BRANCH_ON_M || best_pos < 1.0 {
                continue;
            }
            let (dx, dy) = (nxt[0] - e[0], nxt[1] - e[1]);
            let dl = dx.dhypot(dy);
            if dl < 1e-9 {
                continue;
            }
            let (dx, dy) = (dx / dl, dy / dl);
            let fwd = dx * best_t[0] + dy * best_t[1];
            let cr = best_t[0] * dy - best_t[1] * dx;
            let at_end = terminal && best_pos >= line_len - 2.5;
            if fwd >= 0.95 && cr.abs() < BRANCH_SIN {
                straight_at_end |= at_end;
            } else if fwd >= 0.3 && cr.abs() >= BRANCH_SIN {
                diverging.push((best_pos, if cr > 0.0 { 1 } else { -1 }, at_end));
            }
        }
    }
    diverging.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    for (_pos, side, at_end) in diverging {
        // (a diverging end at the track's own end is a switch only where a straight continuation leaves it too)
        if !at_end || straight_at_end {
            return side;
        }
    }
    0
}

fn cm(p: [f64; 2]) -> (i64, i64) {
    ((p[0] * 100.0).round() as i64, (p[1] * 100.0).round() as i64)
}

/// The junction of crossing point p: (the key node, rule) or None. A traffic signals node within NEAR_M qualifies it
/// (the key: the smallest node within CLUSTER_M, its arms' signals), else an OSM railway=signal node within
/// RAIL_SIGNAL_NEAR_M (the key: the nearest such node, ties to the smaller).
fn junction(p: [f64; 2], traffic: &[[f64; 2]], rail: &[[f64; 2]]) -> Option<([f64; 2], u8)> {
    let d = |q: &[f64; 2]| (q[0] - p[0]).dhypot(q[1] - p[1]);
    if traffic.iter().any(|q| d(q) <= NEAR_M) {
        let key = traffic.iter().filter(|q| d(q) <= CLUSTER_M).min_by_key(|q| cm(**q));
        return key.map(|k| (*k, 0));
    }
    let mut best: Option<([f64; 2], f64)> = None;
    for q in rail.iter().filter(|q| d(q) <= RAIL_SIGNAL_NEAR_M) {
        let dq = d(q);
        if best.map_or(true, |(k, dk)| dq < dk - 1e-9 || ((dq - dk).abs() <= 1e-9 && cm(*q) < cm(k))) {
            best = Some((*q, dq));
        }
    }
    best.map(|(k, _)| (k, 1))
}

/// The tram signals of the tram tracks `parts` (ground tracks, polylines in the frame's metres) over the carriageways
/// `roads` ((centreline, half width)). traffic: OSM highway=traffic_signals nodes, rail: OSM railway=signal nodes on tram
/// lines, left_hand: the traffic side, country: for the face style. Sorted and de-duplicated: the same set in every tile
/// that reads these inputs (the caller keeps the ones its tile owns).
pub fn place(parts: &[Pts], roads: &[(Pts, f64)], traffic: &[[f64; 2]], rail: &[[f64; 2]], left_hand: bool,
             country: &str) -> Vec<Signal> {
    let style = style_of(country);
    let part_arcs: Vec<Vec<f64>> = parts.iter().map(|p| arcs(p)).collect();
    let part_box: Vec<[f64; 4]> = parts.iter().map(|p| bbox(p)).collect();
    let road_box: Vec<[f64; 4]> = roads.iter().map(|(r, _)| bbox(r)).collect();
    // one crossing of a part and a road per metre cell: a vertex shared by two segments is one
    let mut seen: Vec<(usize, usize, i64, i64)> = Vec::new();
    let mut found: Vec<Signal> = Vec::new();
    for (pi, part) in parts.iter().enumerate() {
        if part.len() < 2 {
            continue;
        }
        for (ri, (road, hw)) in roads.iter().enumerate() {
            if !overlap(part_box[pi], road_box[ri]) {
                continue;
            }
            for (j, wa) in part.windows(2).enumerate() {
                for wb in road.windows(2) {
                    let Some((along, p, sin)) = cross(wa[0], wa[1], wb[0], wb[1]) else { continue };
                    if sin < MIN_SIN {
                        continue;
                    }
                    let id = (pi, ri, p[0].round() as i64, p[1].round() as i64);
                    if seen.contains(&id) {
                        continue;
                    }
                    seen.push(id);
                    let Some((jkey, rule)) = junction(p, traffic, rail) else { continue };
                    let s0 = part_arcs[pi][j] + along;
                    let before = hw / sin + BEFORE_M;
                    let (cycle, offset, green) = cycle_of(jkey, rule);
                    for sgn in [1.0f64, -1.0] {
                        let Some((q, tq)) = at_arc(part, &part_arcs[pi], s0 - sgn * before) else { continue };
                        let a = [tq[0] * sgn, tq[1] * sgn]; // the travel direction towards the crossing
                        let right = [a[1], -a[0]];
                        let side = if left_hand { [-right[0], -right[1]] } else { right };
                        // another track on this side within PAIR_M: this direction's post is on the outer side of the pair
                        let mut paired = false;
                        let mut d = LATERAL_M;
                        while d <= PAIR_M + 1e-9 && !paired {
                            let c = [q[0] + side[0] * d, q[1] + side[1] * d];
                            paired = parts.iter().enumerate().any(|(oi, o)| {
                                oi != pi && o.len() >= 2
                                    && overlap(part_box[oi], [c[0] - 3.0, c[1] - 3.0, c[0] + 3.0, c[1] + 3.0])
                                    && dist_line(c, o) < TRACK_CLEAR_M
                            });
                            d += LATERAL_STEP_M;
                        }
                        if paired {
                            continue;
                        }
                        let mut d = LATERAL_M;
                        let mut spot = None;
                        while d <= LATERAL_MAX_M + 1e-9 {
                            let c = [q[0] + side[0] * d, q[1] + side[1] * d];
                            if roads.iter().all(|(r, w)| dist_line(c, r) > w + ROAD_OFF_M) {
                                spot = Some(c);
                                break;
                            }
                            d += LATERAL_STEP_M;
                        }
                        let Some(c) = spot else { continue };
                        // the model's +x is the right of the travel, the face its -y: towards the tram
                        let yaw = right[1].datan2(right[0]).to_degrees().rem_euclid(360.0);
                        let branch = branch_side(parts, pi, &part_arcs[pi], s0 - sgn * before, sgn);
                        found.push(Signal { x: c[0], y: c[1], yaw, style, cycle, offset, green, key: jkey, rule, branch });
                    }
                }
            }
        }
    }
    found.sort_by_key(|s| (cm([s.x, s.y]), (s.yaw * 10.0).round() as i64));
    let mut out: Vec<Signal> = Vec::new();
    for s in found {
        if out.iter().all(|o| (o.x - s.x).dhypot(o.y - s.y) >= MERGE_M) {
            out.push(s);
        }
    }
    out
}

fn polylines(pts: &PyReadonlyArray2<'_, f64>, off: &PyReadonlyArray1<'_, i64>) -> PyResult<Vec<Pts>> {
    let a = pts.as_array();
    if a.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_signals: points need two columns"));
    }
    let offv: Vec<i64> = off.as_array().iter().copied().collect();
    if offv.is_empty() || offv.iter().any(|&o| o < 0 || o as usize > a.nrows()) || offv.windows(2).any(|w| w[1] < w[0]) {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_signals: inconsistent offsets"));
    }
    Ok(offv.windows(2).map(|w| (w[0] as usize..w[1] as usize).map(|i| [a[[i, 0]], a[[i, 1]]]).collect()).collect())
}

fn points(a: &PyReadonlyArray2<'_, f64>) -> Vec<[f64; 2]> {
    let v = a.as_array();
    if v.ncols() < 2 {
        return Vec::new();
    }
    v.rows().into_iter().map(|r| [r[0], r[1]]).collect()
}

/// The tram signals (see `place`): tracks pts (n, 2) with offsets off (k + 1), roads rpts (m, 2) with offsets roff
/// (j + 1) and half widths rhw (j,), traffic (a, 2) and rail (b, 2) signal nodes. Returns (s, 11) rows: x, y, yaw
/// (degrees), style, cycle, offset, green (s), key x, key y, rule, branch (1 left, -1 right, 0 none).
#[pyfunction]
#[pyo3(signature = (pts, off, rpts, roff, rhw, traffic, rail, left_hand, country))]
#[allow(clippy::too_many_arguments)]
pub fn tram_signals<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                         rpts: PyReadonlyArray2<'py, f64>, roff: PyReadonlyArray1<'py, i64>,
                         rhw: PyReadonlyArray1<'py, f64>, traffic: PyReadonlyArray2<'py, f64>,
                         rail: PyReadonlyArray2<'py, f64>, left_hand: bool, country: &str)
                         -> PyResult<Bound<'py, PyArray2<f64>>> {
    let parts = polylines(&pts, &off)?;
    let rlines = polylines(&rpts, &roff)?;
    let hw: Vec<f64> = rhw.as_array().iter().copied().collect();
    if hw.len() != rlines.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_signals: one half width per road"));
    }
    let roads: Vec<(Pts, f64)> = rlines.into_iter().zip(hw).collect();
    let (tr, ra) = (points(&traffic), points(&rail));
    let country = country.to_string();
    let found = py.detach(move || place(&parts, &roads, &tr, &ra, left_hand, &country));
    let mut flat = Vec::with_capacity(found.len() * 11);
    for s in &found {
        flat.extend_from_slice(&[s.x, s.y, s.yaw, s.style as f64, s.cycle, s.offset, s.green, s.key[0], s.key[1],
                                 s.rule as f64, s.branch as f64]);
    }
    let arr = Array2::from_shape_vec((found.len(), 11), flat)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(arr.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(a: [f64; 2], b: [f64; 2], step: f64) -> Pts {
        let n = ((b[0] - a[0]).dhypot(b[1] - a[1]) / step).ceil().max(1.0) as usize;
        (0..=n).map(|i| {
            let t = i as f64 / n as f64;
            [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
        }).collect()
    }

    // the tram along x through the origin, a 7 m wide road along y, a signalled node 12 m off
    fn base() -> (Vec<Pts>, Vec<(Pts, f64)>, Vec<[f64; 2]>) {
        (vec![line([-80.0, 0.0], [80.0, 0.0], 2.0)], vec![(line([0.0, -80.0], [0.0, 80.0], 2.0), 3.5)], vec![[12.0, 8.0]])
    }

    #[test]
    fn one_signal_per_direction_before_the_stop_line_right_hand() {
        let (p, r, t) = base();
        let out = place(&p, &r, &t, &[], false, "DE");
        assert_eq!(out.len(), 2);
        // heading +x: before the road (x < 0), on the right (y < 0), the model's +x is the right: -y = 270 degrees
        let a = out.iter().find(|s| s.x < 0.0).unwrap();
        assert!((a.x + 7.0).abs() < 1e-6 && (a.y + 2.0).abs() < 1e-6, "{a:?}");
        assert!((a.yaw - 270.0).abs() < 1e-6);
        // heading -x: x > 0, on its right (y > 0), yaw 90
        let b = out.iter().find(|s| s.x > 0.0).unwrap();
        assert!((b.x - 7.0).abs() < 1e-6 && (b.y - 2.0).abs() < 1e-6);
        assert!((b.yaw - 90.0).abs() < 1e-6);
        assert_eq!(a.style, STYLE_BOSTRAB);
        assert_eq!((a.cycle, a.offset, a.green), (b.cycle, b.offset, b.green));
    }

    #[test]
    fn left_hand_traffic_mirrors_the_post_side() {
        let (p, r, t) = base();
        let out = place(&p, &r, &t, &[], true, "GB");
        let a = out.iter().find(|s| s.x < 0.0).unwrap();
        assert!((a.y - 2.0).abs() < 1e-6, "{a:?}");
        assert!((a.yaw - 270.0).abs() < 1e-6, "the face still looks at the tram: {a:?}");
    }

    #[test]
    fn needs_a_signalled_junction() {
        let (p, r, _) = base();
        assert!(place(&p, &r, &[], &[], false, "DE").is_empty());
        assert!(place(&p, &r, &[[80.0, 40.0]], &[], false, "DE").is_empty()); // a signal elsewhere
        assert_eq!(place(&p, &r, &[[0.0, NEAR_M - 0.1]], &[], false, "DE").len(), 2);
        assert!(place(&p, &r, &[[0.0, NEAR_M + 0.1]], &[], false, "DE").is_empty());
    }

    #[test]
    fn a_railway_signal_node_is_the_other_rule() {
        let (p, r, _) = base();
        let out = place(&p, &r, &[], &[[5.0, 3.0]], false, "DE");
        assert_eq!(out.len(), 2);
        assert!(out.iter().all(|s| s.rule == 1 && s.key == [5.0, 3.0]));
        assert!(place(&p, &r, &[], &[[5.0, RAIL_SIGNAL_NEAR_M + 1.0]], false, "DE").is_empty());
    }

    #[test]
    fn a_road_along_the_track_is_no_crossing() {
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0)];
        let r = vec![(line([-80.0, 2.0], [80.0, 12.0], 2.0), 3.5)]; // 5.7 degrees: sin 0.1
        assert!(place(&p, &r, &[[0.0, 5.0]], &[], false, "DE").is_empty());
    }

    #[test]
    fn a_skew_crossing_stands_further_back() {
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0)];
        let r = vec![(line([-40.0, -40.0], [40.0, 40.0], 2.0), 3.5)]; // 45 degrees
        let out = place(&p, &r, &[[10.0, 10.0]], &[], false, "DE");
        assert_eq!(out.len(), 2);
        let a = out.iter().find(|s| s.x < 0.0).unwrap();
        let want = 3.5 / (0.5f64).sqrt() + BEFORE_M;
        assert!((a.x + want).abs() < 1e-6, "{a:?} {want}");
    }

    #[test]
    fn a_pair_of_tracks_keeps_the_outer_sides() {
        let (_, r, t) = base();
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0), line([-80.0, 3.0], [80.0, 3.0], 2.0)];
        let out = place(&p, &r, &t, &[], false, "DE");
        assert_eq!(out.len(), 2, "{out:?}");
        // right-hand traffic: the lower track runs +x (its post at y = -2), the upper -x (post at y = 5)
        assert!(out.iter().any(|s| s.x < 0.0 && (s.y + 2.0).abs() < 1e-6));
        assert!(out.iter().any(|s| s.x > 0.0 && (s.y - 5.0).abs() < 1e-6));
    }

    #[test]
    fn a_wide_pair_still_keeps_the_outer_sides() {
        // the tracks 5.7 m apart (a median): each post is on its own outer side, none in the middle
        let (_, r, t) = base();
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0), line([-80.0, 5.7], [80.0, 5.7], 2.0)];
        let out = place(&p, &r, &t, &[], false, "DE");
        assert_eq!(out.len(), 2, "{out:?}");
        assert!(out.iter().any(|s| s.x < 0.0 && (s.y + 2.0).abs() < 1e-6));
        assert!(out.iter().any(|s| s.x > 0.0 && (s.y - 7.7).abs() < 1e-6));
        // tracks 20 m apart are two lines, each with both directions
        let far = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0), line([-80.0, 20.0], [80.0, 20.0], 2.0)];
        assert_eq!(place(&far, &r, &t, &[], false, "DE").len(), 4);
    }

    #[test]
    fn a_track_in_the_street_puts_its_post_beside_the_lanes() {
        // the street along x, 14 m wide, the track down its middle; a side street along y with the signal
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0)];
        let r = vec![(line([-80.0, 0.0], [80.0, 0.0], 2.0), 7.0), (line([0.0, -80.0], [0.0, 80.0], 2.0), 3.5)];
        let out = place(&p, &r, &[[0.0, 12.0]], &[], false, "DE");
        assert!(!out.is_empty());
        for s in &out {
            assert!(s.y.abs() >= 7.0 + ROAD_OFF_M - 1e-6 && s.y.abs() <= LATERAL_MAX_M + 1e-6, "{s:?}");
            for (road, hw) in &r {
                assert!(dist_line([s.x, s.y], road) > hw + ROAD_OFF_M - 1e-6);
            }
        }
    }

    #[test]
    fn the_cycle_is_the_junctions_and_deterministic() {
        let k = [123.45, -67.89];
        assert_eq!(cycle_of(k, 0), cycle_of(k, 0));
        for i in 0..200 {
            let (c, o, g) = cycle_of([i as f64 * 3.7, 1000.0 - i as f64], 0);
            assert!((60.0..=90.0).contains(&c) && (c - 60.0) % 5.0 == 0.0, "{c}");
            assert!(o >= 0.0 && o < c && o.fract() == 0.0);
            assert!((12.0..=18.0).contains(&g));
        }
        let cycles: std::collections::BTreeSet<i64> =
            (0..200).map(|i| cycle_of([i as f64 * 3.7, 5.0], 0).0 as i64).collect();
        assert!(cycles.len() >= 5);
        // pinned: a change of the hash would move every signal's phase in every saved place
        assert_eq!(cycle_of([123.45, -67.89], 0), PINNED);
    }

    const PINNED: (f64, f64, f64) = (65.0, 45.0, 13.0);

    #[test]
    fn the_key_is_the_smallest_node_so_each_arm_agrees() {
        let (p, r, _) = base();
        let nodes = [[12.0, 8.0], [-14.0, 6.0], [3.0, -10.0]];
        let out = place(&p, &r, &nodes, &[], false, "DE");
        assert!(out.iter().all(|s| s.key == [-14.0, 6.0]));
        // the order of the nodes does not matter
        let rev = [nodes[2], nodes[0], nodes[1]];
        assert_eq!(place(&p, &r, &rev, &[], false, "DE"), out);
    }

    #[test]
    fn the_style_follows_the_country() {
        assert_eq!(style_of("UA"), STYLE_BAR);
        assert_eq!(style_of("cz"), STYLE_BAR);
        assert_eq!(style_of("DE"), STYLE_BOSTRAB);
        assert_eq!(style_of("FI"), STYLE_BOSTRAB);
        assert_eq!(style_of(""), STYLE_BOSTRAB);
    }

    fn dir(a: [f64; 2], deg: f64, len: f64) -> Pts {
        let r = deg.to_radians();
        line(a, [a[0] + len * r.dcos(), a[1] + len * r.dsin()], 2.0)
    }

    #[test]
    fn a_branch_ahead_gives_the_side_it_leaves_to_only_for_the_tram_heading_to_it() {
        // a T: a second track's end on the through track at x = 25, leaving at +20 degrees (left of the heading +x)
        let (_, r, t) = base();
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0), dir([25.0, 0.0], 20.0, 60.0)];
        let out = place(&p, &r, &t, &[], false, "DE");
        let east = out.iter().find(|s| s.x < 0.0 && s.y < 0.0).expect("the signal of the tram heading east");
        assert_eq!(east.branch, 1, "{out:?}");
        let west = out.iter().find(|s| s.x > 0.0 && s.y > 0.0).expect("the signal of the tram heading west");
        assert_eq!(west.branch, 0, "the branch is behind a tram heading west: {out:?}");
        // the same branch to the right
        let p = vec![line([-80.0, 0.0], [80.0, 0.0], 2.0), dir([25.0, 0.0], -20.0, 60.0)];
        let out = place(&p, &r, &t, &[], false, "DE");
        assert_eq!(out.iter().find(|s| s.x < 0.0 && s.y < 0.0).unwrap().branch, -1);
    }

    #[test]
    fn no_branch_for_a_continuation_a_far_branch_or_a_parallel_track() {
        let (_, r, t) = base();
        // the same line in two ways: a continuation, not a branch
        let p = vec![line([-80.0, 0.0], [30.0, 0.0], 2.0), line([30.0, 0.0], [80.0, 0.0], 2.0)];
        assert!(place(&p, &r, &t, &[], false, "DE").iter().all(|s| s.branch == 0));
        // a curve of the same line into another way: no straight continuation, not a switch
        let p = vec![line([-80.0, 0.0], [30.0, 0.0], 2.0), dir([30.0, 0.0], 25.0, 60.0)];
        assert!(place(&p, &r, &t, &[], false, "DE").iter().all(|s| s.branch == 0));
        // a branch 100 m on is not this junction's
        let p = vec![line([-80.0, 0.0], [160.0, 0.0], 2.0), dir([120.0, 0.0], 20.0, 40.0)];
        assert!(place(&p, &r, &t, &[], false, "DE").iter().all(|s| s.branch == 0));
    }

    #[test]
    fn a_y_switch_at_the_track_end_branches_to_its_side() {
        // the through track ends at x = 25 where a straight way and a way leaving to the right begin
        let (_, r, t) = base();
        let p = vec![line([-80.0, 0.0], [25.0, 0.0], 2.0), line([25.0, 0.0], [90.0, 0.0], 2.0), dir([25.0, 0.0], -15.0, 60.0)];
        let out = place(&p, &r, &t, &[], false, "DE");
        let east = out.iter().find(|s| s.x < 0.0 && s.y < 0.0).expect("east signal");
        assert_eq!(east.branch, -1, "{out:?}");
    }

    #[test]
    fn a_track_that_ends_at_the_junction_has_no_signal_behind_it() {
        // the track starts at the road: nothing before the stop line on that side
        let p = vec![line([0.0, 0.0], [80.0, 0.0], 2.0)];
        let (_, r, t) = base();
        let out = place(&p, &r, &t, &[], false, "DE");
        assert!(!out.is_empty() && out.iter().all(|s| s.x > 0.0));
    }
}
