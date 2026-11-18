//! Lane tapers (server roads.py road_tapers): where a road goes on as one of another width, or onto its own deck, the
//! joint and the width it runs to there. See roads.py TAPER_* for the rules; this is their kernel.
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

/// Design speed (km/h) of a road for its tapers (the road revamp's plan §2.3; R1, 2026-11-16): its speed limit (kmh,
/// NaN or <= 0: none) up to 130, else by its class rank (0 motorway, 1 trunk, 2 primary, 3 secondary, 4 tertiary,
/// 5 unclassified / residential / other streets, 6 service / track) rural or urban; a link 0.7 of its class's, at
/// least 40.
pub(crate) fn design_speed_kmh(kmh: f64, rank: f64, urban: bool, link: bool) -> f64 {
    const RURAL: [f64; 7] = [120.0, 100.0, 90.0, 80.0, 70.0, 50.0, 30.0];
    const URBAN: [f64; 7] = [90.0, 70.0, 50.0, 50.0, 50.0, 30.0, 20.0];
    if kmh.is_finite() && kmh > 0.0 {
        return kmh.min(130.0);
    }
    let r = if rank.is_finite() { rank.clamp(0.0, 6.0) as usize } else { 5 };
    let v = if urban { URBAN[r] } else { RURAL[r] };
    if link { (0.7 * v).max(40.0) } else { v }
}

/// A taper's length (m) for a lateral change of w m at v km/h (plan §2.3): MUTCD's merging taper W·V·min(V/155, 0.62)
/// (W·V²/155 under ~96 km/h, 0.62·W·V above, without the jump at 45 mph), at least what keeps the quintic shape's
/// lateral acceleration at 0.5 m/s² (sqrt(5.77·W·v²/0.5)), within 20-400 m: 54 m at 30 km/h to 268 m at 120 km/h for a
/// 3.6 m lane.
pub(crate) fn speed_taper_m(w: f64, v_kmh: f64) -> f64 {
    let l_std = w * v_kmh * (v_kmh / 155.0).min(0.62);
    let v = v_kmh / 3.6;
    let l_acc = (5.77 * w * v * v / 0.5).sqrt();
    l_std.max(l_acc).clamp(20.0, 400.0)
}

/// lonlat (P, 2) of the roads' lines, off (n + 1) into it; attrs (n, 5 or 9): width (NaN: no ground spec), class code
/// (< 0: none), deck at start (0/1), deck at end (0/1), deck width (NaN: no bridge), and in speed mode its speed limit
/// (km/h, NaN none), class rank, urban (0/1), link (0/1) (design_speed_kmh); own (R, 2): (road, fraction along it) of
/// its own deck's ends inside it (roads taper only where two meet); params: m per m, shortest, longest, share of the
/// road, least change, most turn (deg), and (optional) 1: deck mode, a deck's end meeting another deck's end to end, both
/// tapering to half way (no own rows), and (optional) 1: speed mode (BNE_SPEED_TAPERS: each taper as long as
/// speed_taper_m for the change on its own side at the road's design speed, the share of the road at most, and its
/// line given). Returns ((T, 5): road, lon, lat, width at the joint, length (m), in road order then end 0, end 1, own
/// decks; line_off (T + 1), line_ll (Q, 2), s_joint (T,)): in speed mode each taper's stretch of its road's line (its
/// points within its length of the joint, the vertices just past) and the joint's distance along that stretch (m), for
/// taper_widths_along; else empty.
#[pyfunction]
pub fn road_tapers<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                        attrs: PyReadonlyArray2<'py, f64>, own: PyReadonlyArray2<'py, f64>,
                        params: PyReadonlyArray1<'py, f64>)
                        -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>,
                                     Bound<'py, PyArray1<f64>>)> {
    let (ll, off, at, own, p) = (lonlat.as_array(), off.as_array(), attrs.as_array(), own.as_array(), params.as_array());
    let (m_per_m, min_l, max_l, share, min_dw, max_turn) = (p[0], p[1], p[2], p[3], p[4], p[5]);
    let decks = p.len() > 6 && p[6] > 0.5;     // (deck mode: decks meeting decks, at their deck widths)
    let speed = p.len() > 7 && p[7] > 0.5;     // (speed mode: BNE_SPEED_TAPERS)
    if speed && at.ncols() < 9 {
        return Err(pyo3::exceptions::PyValueError::new_err("road_tapers: speed mode needs attrs (n, 9)"));
    }
    let n = off.len().saturating_sub(1);
    let (rows, lines) = py.detach(|| {
        let mut rows: Vec<[f64; 5]> = Vec::new();
        // per row in speed mode: (road, the joint's distance from the road's start, m)
        let mut joints: Vec<(usize, f64)> = Vec::new();
        let ok: Vec<bool> = (0..n).map(|i| (off[i + 1] - off[i]) >= 2 && at[[i, 1]] >= 0.0).collect();
        if !ok.iter().any(|&b| b) {
            return (rows, Vec::new());
        }
        // metres per degree at the roads' mean first latitude (as the Python: one scale for the cell)
        let (mut lat_sum, mut cnt) = (0.0, 0.0);
        for i in 0..n {
            if ok[i] {
                lat_sum += ll[[off[i] as usize, 1]];
                cnt += 1.0;
            }
        }
        let lat0 = lat_sum / cnt;
        let (mx, my) = (111320.0 * lat0.to_radians().dcos(), 110540.0);
        let key = |lon: f64, lat: f64| ((lon * 1e6).round() as i64, (lat * 1e6).round() as i64);
        let mut ends: HashMap<(i64, i64), Vec<(usize, usize)>> = HashMap::new();
        for i in 0..n {
            if !ok[i] {
                continue;
            }
            let (a, b) = (off[i] as usize, off[i + 1] as usize - 1);
            ends.entry(key(ll[[a, 0]], ll[[a, 1]])).or_default().push((i, 0));
            ends.entry(key(ll[[b, 0]], ll[[b, 1]])).or_default().push((i, 1));
        }
        // unit direction away from end e over its first ~10 m, and the line's length
        let leaving = |i: usize, e: usize| -> (Option<[f64; 2]>, f64) {
            let (a, b) = (off[i] as usize, off[i + 1] as usize);
            let idx = |k: usize| if e == 0 { a + k } else { b - 1 - k };
            let (x0, y0) = (ll[[idx(0), 0]], ll[[idx(0), 1]]);
            let (mut s, mut dir, mut px, mut py_) = (0.0, None, 0.0, 0.0);
            for k in 1..(b - a) {
                let (x, y) = ((ll[[idx(k), 0]] - x0) * mx, (ll[[idx(k), 1]] - y0) * my);
                s += (x - px).dhypot(y - py_);
                px = x;
                py_ = y;
                if dir.is_none() && s >= 10.0 {
                    dir = Some([x, y]);
                }
            }
            let v = dir.unwrap_or([px, py_]);
            let l = v[0].dhypot(v[1]);
            (if l > 1e-6 { Some([v[0] / l, v[1] / l]) } else { None }, s)
        };
        let cos_max = max_turn.to_radians().dcos();
        // (dw: the width change the joint makes, side: the part of it on this road's side)
        let span = |i: usize, dw: f64, side: f64, length: f64| {
            if speed {
                let v = design_speed_kmh(at[[i, 5]], at[[i, 6]], at[[i, 7]] > 0.5, at[[i, 8]] > 0.5);
                speed_taper_m(side, v).min(share * length)
            } else {
                (dw * m_per_m).clamp(min_l, max_l).min(share * length)
            }
        };
        for i in 0..n {
            let w = if decks { at[[i, 4]] } else { at[[i, 0]] };
            if !ok[i] || w.is_nan() {
                continue;                       // (all deck: it keeps its width; deck mode: no deck)
            }
            for e in 0..2usize {
                if (at[[i, 2 + e]] > 0.5) != decks {
                    continue;                   // (a deck at its end: its width stands, the other road tapers; in deck
                }                               // mode only a deck's end, meeting another deck's)
                let v = if e == 0 { off[i] as usize } else { off[i + 1] as usize - 1 };
                let (lon, lat) = (ll[[v, 0]], ll[[v, 1]]);
                let (hi, length) = leaving(i, e);
                let Some(hi) = hi else { continue };
                let mut best: Option<(f64, usize, usize)> = None;
                let mut meeting = 0usize;
                if let Some(list) = ends.get(&key(lon, lat)) {
                    meeting = list.len();
                    for &(j, ej) in list {
                        if j == i || at[[j, 1]] != at[[i, 1]] {
                            continue;
                        }
                        let (hj, _) = leaving(j, ej);
                        let Some(hj) = hj else { continue };
                        let dot = hi[0] * hj[0] + hi[1] * hj[1];
                        if dot <= -cos_max && best.map_or(true, |b| dot < b.0) {
                            best = Some((dot, j, ej));
                        }
                    }
                }
                let Some((_, j, ej)) = best else { continue };
                let deck = !at[[j, 4]].is_nan() && at[[j, 2 + ej]] > 0.5;
                if decks {
                    // two decks end to end: both run to half way between their widths
                    if !deck || meeting > 2 || (w - at[[j, 4]]).abs() < min_dw {
                        continue;
                    }
                    let wj = at[[j, 4]];
                    rows.push([i as f64, lon, lat, 0.5 * (w + wj), span(i, (w - wj).abs(), 0.5 * (w - wj).abs(), length)]);
                    joints.push((i, if e == 0 { 0.0 } else { length }));
                    continue;
                }
                let wj = if deck { at[[j, 4]] } else { at[[j, 0]] };
                if wj.is_nan() || (w - wj).abs() < min_dw {
                    continue;
                }
                if meeting > 2 {
                    // a junction: the width changes across it, as real streets' do; a deck's end where it splits into
                    // the carriageways of a divided road (East Pratt Street's 21.8 m deck into two 12 m ones): each
                    // widened to the deck's width over the other and the river bank, under the trees beside them
                    continue;
                }
                let target = if deck { wj } else { 0.5 * (w + wj) };
                rows.push([i as f64, lon, lat, target, span(i, (w - wj).abs(), (w - target).abs(), length)]);
                joints.push((i, if e == 0 { 0.0 } else { length }));
            }
        }
        // its own deck on a range of it: the ground either side runs to the deck's width
        for r in 0..(if decks { 0 } else { own.nrows() }) {
            let (i, f) = (own[[r, 0]] as usize, own[[r, 1]]);
            if i >= n || !ok[i] {
                continue;
            }
            let (w, bw) = (at[[i, 0]], at[[i, 4]]);
            if w.is_nan() || bw.is_nan() || (w - bw).abs() < min_dw {
                continue;
            }
            let (a, b) = (off[i] as usize, off[i + 1] as usize);
            let mut s = vec![0.0f64; b - a];
            for k in 1..(b - a) {
                s[k] = s[k - 1] + ((ll[[a + k, 0]] - ll[[a + k - 1, 0]]) * mx).dhypot((ll[[a + k, 1]] - ll[[a + k - 1, 1]]) * my);
            }
            let total = s[b - a - 1];
            let t = f * total;
            let k = s.partition_point(|&x| x < t).clamp(1, b - a - 1);
            let u = if s[k] > s[k - 1] { (t - s[k - 1]) / (s[k] - s[k - 1]) } else { 0.0 };
            let lon = ll[[a + k - 1, 0]] + u * (ll[[a + k, 0]] - ll[[a + k - 1, 0]]);
            let lat = ll[[a + k - 1, 1]] + u * (ll[[a + k, 1]] - ll[[a + k - 1, 1]]);
            rows.push([i as f64, lon, lat, bw, span(i, (w - bw).abs(), (w - bw).abs(), total)]);
            joints.push((i, t));
        }
        // speed mode: each taper's stretch of its road's line, within its length of the joint (the vertices just past
        // it kept), and the joint's distance along the stretch
        let mut lines: Vec<(Vec<[f64; 2]>, f64)> = Vec::new();
        if speed {
            for (r, &(i, sj)) in joints.iter().enumerate() {
                let span_m = rows[r][4];
                let (a, b) = (off[i] as usize, off[i + 1] as usize);
                let mut s = vec![0.0f64; b - a];
                for k in 1..(b - a) {
                    s[k] = s[k - 1] + ((ll[[a + k, 0]] - ll[[a + k - 1, 0]]) * mx)
                        .dhypot((ll[[a + k, 1]] - ll[[a + k - 1, 1]]) * my);
                }
                let (lo, hi) = (sj - span_m, sj + span_m);
                let first = s.partition_point(|&x| x <= lo).saturating_sub(1);
                let last = s.partition_point(|&x| x < hi).min(b - a - 1);
                let pts: Vec<[f64; 2]> = (first..=last).map(|k| [ll[[a + k, 0]], ll[[a + k, 1]]]).collect();
                lines.push((pts, sj - s[first]));
            }
        }
        (rows, lines)
    });
    let flat: Vec<f64> = rows.iter().flatten().copied().collect();
    let mut line_off = vec![0i64; rows.len() + 1];
    let mut line_ll: Vec<f64> = Vec::new();
    let mut s_joint = vec![0.0f64; rows.len()];
    for (r, (pts, sj)) in lines.iter().enumerate() {
        line_ll.extend(pts.iter().flatten().copied());
        line_off[r + 1] = line_off[r] + pts.len() as i64;
        s_joint[r] = *sj;
    }
    if lines.is_empty() {
        line_off.iter_mut().for_each(|v| *v = 0);
    }
    let q = line_ll.len() / 2;
    Ok((numpy::ndarray::Array2::from_shape_vec((rows.len(), 5), flat).unwrap().into_pyarray(py),
        line_off.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((q, 2), line_ll).unwrap().into_pyarray(py),
        s_joint.into_pyarray(py)))
}

/// The quintic smootherstep 10u³ - 15u⁴ + 6u⁵ (u in [0, 1]): curvature continuous at both ends (plan §2.3), + and ×
/// only.
#[inline]
pub(crate) fn smootherstep(u: f64) -> f64 {
    let u = u.clamp(0.0, 1.0);
    u * u * u * (10.0 + u * (-15.0 + 6.0 * u))
}

/// roads.taper_widths for the speed tapers (BNE_SPEED_TAPERS): per point (lon, lat (P,)) of a road `width` m wide, the
/// width with these tapers: rows (T, 4): width at the joint, length (m), the joint's distance along its line stretch
/// (m), the joint's latitude; line_off (T + 1) / line_ll (Q, 2): each taper's stretch of the road's line (road_tapers'
/// speed mode). A point's distance from the joint is measured along the road (its nearest point on the stretch), not
/// in a straight line, and the width runs from the joint's to the road's own over the length by the quintic
/// smootherstep. Returns ((P,) widths, whether any point is within a taper).
#[pyfunction]
pub fn taper_widths_along<'py>(py: Python<'py>, width: f64, rows: PyReadonlyArray2<'py, f64>,
                               line_off: PyReadonlyArray1<'py, i64>, line_ll: PyReadonlyArray2<'py, f64>,
                               lon: PyReadonlyArray1<'py, f64>, lat: PyReadonlyArray1<'py, f64>)
                               -> PyResult<(Bound<'py, numpy::PyArray1<f64>>, bool)> {
    let (rows, lo, ll, plon, plat) = (rows.as_array(), line_off.as_array(), line_ll.as_array(), lon.as_array(),
                                      lat.as_array());
    let t = rows.nrows();
    if rows.ncols() < 4 || lo.len() != t + 1 || plon.len() != plat.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("taper_widths_along: arrays of different lengths"));
    }
    let np = plon.len();
    let (out, hit) = py.detach(|| {
        let mut out = vec![width; np];
        let mut hit = false;
        for r in 0..t {
            let (target, span, sj, tlat) = (rows[[r, 0]], rows[[r, 1]], rows[[r, 2]], rows[[r, 3]]);
            let (a, b) = (lo[r] as usize, lo[r + 1] as usize);
            if !(span > 0.0) || b < a + 1 {
                continue;
            }
            let (mx, my) = (111320.0 * tlat.to_radians().dcos(), 110540.0);
            let (x0, y0) = (ll[[a, 0]], ll[[a, 1]]);
            let xy: Vec<[f64; 2]> = (a..b).map(|k| [(ll[[k, 0]] - x0) * mx, (ll[[k, 1]] - y0) * my]).collect();
            let mut sv = vec![0.0f64; xy.len()];
            for k in 1..xy.len() {
                sv[k] = sv[k - 1] + (xy[k][0] - xy[k - 1][0]).dhypot(xy[k][1] - xy[k - 1][1]);
            }
            for q in 0..np {
                let px = (plon[q] - x0) * mx;
                let py_ = (plat[q] - y0) * my;
                // the point's station: its nearest point on the stretch (first of equals)
                let (mut best, mut st) = (f64::INFINITY, 0.0);
                if xy.len() == 1 {
                    st = 0.0;
                } else {
                    for k in 0..xy.len() - 1 {
                        let (dx, dy) = (xy[k + 1][0] - xy[k][0], xy[k + 1][1] - xy[k][1]);
                        let l2 = dx * dx + dy * dy;
                        let u = if l2 > 0.0 { (((px - xy[k][0]) * dx + (py_ - xy[k][1]) * dy) / l2).clamp(0.0, 1.0) }
                                else { 0.0 };
                        let (fx, fy) = (xy[k][0] + u * dx - px, xy[k][1] + u * dy - py_);
                        let d2 = fx * fx + fy * fy;
                        if d2 < best {
                            best = d2;
                            st = sv[k] + u * (sv[k + 1] - sv[k]);
                        }
                    }
                }
                let d = (st - sj).abs();
                if d < span {
                    hit = true;
                    out[q] += (target - width) * (1.0 - smootherstep(d / span));
                }
            }
        }
        (out, hit)
    });
    Ok((out.into_pyarray(py), hit))
}

/// Lane counts along one-way roads (server roads.py lane_flow): lonlat (P, 2) and off (n + 1) as road_tapers; attrs
/// (n, 3): one-way direction along the geometry (+1 / -1; 0: not counted), the map's lane count (NaN: unknown), a
/// ramp (1: unknown counts as one lane). A road with no count takes the sum of the counts flowing into its start where
/// only it leaves (a merge, or a road going on), else of those flowing out of its end where only it arrives (a split),
/// else a branch's share: what arrives at its split less the other branches (or leaves its merge less the others);
/// repeated until nothing changes (a chain of pieces). A node where a road not counted ends is left alone. Returns (n,)
/// the counts found (NaN: none), at most max_lanes.
#[pyfunction]
pub fn lane_flow<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                      attrs: PyReadonlyArray2<'py, f64>, max_lanes: f64) -> Bound<'py, numpy::PyArray1<f64>> {
    let (ll, off, at) = (lonlat.as_array(), off.as_array(), attrs.as_array());
    let n = off.len().saturating_sub(1);
    let out = py.detach(|| {
        let key = |k: usize| node_key(ll[[k, 0]], ll[[k, 1]]);
        let ends = |i: usize| -> Option<(NodeKey, NodeKey)> {
            let (a, b) = (off[i] as usize, off[i + 1] as usize);
            if b < a + 2 { None } else { Some((key(a), key(b - 1))) }
        };
        let mut road = Vec::new();
        let mut counted = Vec::new();
        for i in 0..n {
            if at[[i, 0]] != 0.0 {
                if let Some((ka, kb)) = ends(i) {
                    road.push(i);
                    counted.push((ka, kb, [at[[i, 0]], at[[i, 1]], at[[i, 2]], at[[i, 3]]]));
                }
            }
        }
        let blockers = (0..n).filter(|&i| at[[i, 0]] == 0.0).filter_map(ends).flat_map(|(a, b)| [a, b]);
        let got = lane_flow_core(&counted, blockers, max_lanes);
        let mut out = vec![f64::NAN; n];
        for (c, &i) in road.iter().enumerate() {
            out[i] = got[c];
        }
        out
    });
    out.into_pyarray(py)
}

/// A node of lane_flow: a road end's lon / lat in micro-degrees, rounded.
pub(crate) type NodeKey = (i64, i64);

/// lane_flow's node of a point.
#[inline]
pub(crate) fn node_key(lon: f64, lat: f64) -> NodeKey {
    ((lon * 1e6).round() as i64, (lat * 1e6).round() as i64)
}

type NodeMap = HashMap<NodeKey, usize, crate::roadlanes::FxBuild>;

/// The least lane count a motorway link's map count is checked from (ramp_lane_rule) ...
pub(crate) const RAMP_RULE_MIN_LANES: f64 = 3.0;
/// ... and by how many lanes it must exceed what its diverge gives.
pub(crate) const RAMP_RULE_EXCESS: f64 = 2.0;

/// A motorway link mapped with more lanes than its diverge can give (the road revamp's R1, 2026-11-16: in place of
/// rampoverrides.py's I-65 Exit 76 correction, a general rule; no location-specific code). counted, blockers as
/// lane_flow_core's; cand[c]: counted road c is a one-way motorway link with RAMP_RULE_MIN_LANES or more mapped lanes and
/// no mapped width or turn:lanes (nothing else says it is that wide). At its start node along its direction, where it
/// leaves a mainline that goes on (two or more roads leave, every road arriving is no link, no road not counted ends
/// there), the diverge gives
/// max(1, the lanes arriving - the lanes of the other roads leaving) (each road's map count, else its class's default;
/// any unknown: no rule). A count that exceeds that by RAMP_RULE_EXCESS or more takes it (I-65 Exit 76 at Upton: lanes=3
/// on a single-lane exit from a 3-lane mainline that goes on with 3). Returns per counted road the count, NaN none.
pub(crate) fn ramp_lane_rule(counted: &[(NodeKey, NodeKey, [f64; 4])], cand: &[bool],
                             blockers: impl IntoIterator<Item = NodeKey>) -> Vec<f64> {
    let n = counted.len();
    let mut out = vec![f64::NAN; n];
    if !cand.iter().any(|&c| c) {
        return out;
    }
    let mut node: NodeMap = HashMap::default();
    let mut tail = vec![usize::MAX; n];
    let mut head = vec![usize::MAX; n];
    for (i, &(ka, kb, a)) in counted.iter().enumerate() {
        let len = node.len();
        let na = *node.entry(ka).or_insert(len);
        let len = node.len();
        let nb = *node.entry(kb).or_insert(len);
        if a[0] > 0.0 { tail[i] = na; head[i] = nb; } else { tail[i] = nb; head[i] = na; }
    }
    let nn = node.len();
    let mut blocked = vec![false; nn];
    for k in blockers {
        if let Some(&j) = node.get(&k) {
            blocked[j] = true;
        }
    }
    let mut ins: Vec<Vec<usize>> = vec![Vec::new(); nn];
    let mut outs: Vec<Vec<usize>> = vec![Vec::new(); nn];
    for i in 0..n {
        outs[tail[i]].push(i);
        ins[head[i]].push(i);
    }
    let lanes = |j: usize| -> f64 { if counted[j].2[1].is_nan() { counted[j].2[3] } else { counted[j].2[1] } };
    for i in 0..n {
        if !cand.get(i).copied().unwrap_or(false) || counted[i].2[1].is_nan() {
            continue;
        }
        let t = tail[i];
        // (only where it leaves a mainline: a ramp splitting from a ramp near its terminal fans out to the lanes it
        // has there, 1 into 3 at a Baltimore County signal, w53142602)
        if blocked[t] || outs[t].len() < 2 || ins[t].is_empty() || ins[t].iter().any(|&j| counted[j].2[2] > 0.5) {
            continue;
        }
        let mut arriving = 0.0;
        let mut others = 0.0;
        let mut known = true;
        for &j in &ins[t] {
            let v = lanes(j);
            known &= !v.is_nan();
            arriving += v;
        }
        for &j in outs[t].iter().filter(|&&j| j != i) {
            let v = lanes(j);
            known &= !v.is_nan();
            others += v;
        }
        if !known {
            continue;
        }
        let gives = (arriving - others).max(1.0);
        let mapped = counted[i].2[1];
        if mapped >= RAMP_RULE_MIN_LANES && mapped - gives >= RAMP_RULE_EXCESS {
            out[i] = gives;
        }
    }
    out
}

/// lane_flow's solve. counted: the counted roads (attrs one-way sign along the geometry not 0, two points or more), in
/// road order: (start node, end node along the geometry, attrs row: one-way sign, the map's lanes or NaN, ramp 1 / 0,
/// the class's default lanes or NaN); blockers: both end nodes of every road of two points or more not counted (its
/// sign 0). Returns the count found per counted road (NaN: none), at most max_lanes; a road not counted never has one
/// (only its ends matter: a node a road not counted ends at is left alone).
pub(crate) fn lane_flow_core(counted: &[(NodeKey, NodeKey, [f64; 4])], blockers: impl IntoIterator<Item = NodeKey>,
                             max_lanes: f64) -> Vec<f64> {
    let n = counted.len();
    let at: Vec<[f64; 4]> = counted.iter().map(|c| c.2).collect();
    let mut node: NodeMap = HashMap::default();
    let mut tail = vec![usize::MAX; n];
    let mut head = vec![usize::MAX; n];
    for (i, &(ka, kb, a)) in counted.iter().enumerate() {
        let len = node.len();
        let na = *node.entry(ka).or_insert(len);
        let len = node.len();
        let nb = *node.entry(kb).or_insert(len);
        if a[0] > 0.0 { tail[i] = na; head[i] = nb; } else { tail[i] = nb; head[i] = na; }
    }
    let nn = node.len();
    let mut blocked = vec![false; nn];      // a node a road not counted ends at
    if nn > 0 {
        for k in blockers {
            if let Some(&j) = node.get(&k) {
                blocked[j] = true;
            }
        }
    }
    let mut ins: Vec<Vec<usize>> = vec![Vec::new(); nn];     // roads whose traffic arrives at the node
    let mut outs: Vec<Vec<usize>> = vec![Vec::new(); nn];    // ... leaves it
    for i in 0..n {
        outs[tail[i]].push(i);
        ins[head[i]].push(i);
    }
    let mut lanes: Vec<f64> = (0..n).map(|i| {
        let v = at[i][1];
        if v.is_nan() && at[i][2] > 0.5 { 1.0 } else { v }
    }).collect();
    let known0: Vec<bool> = lanes.iter().map(|v| !v.is_nan()).collect();
    let mut deflt = vec![false; n];          // (phase 2: counting as its default)
    let mut found_default = vec![false; n];
    let sum = |list: &Vec<usize>, lanes: &Vec<f64>| -> Option<f64> {
        if list.is_empty() { return None; }
        let mut s = 0.0;
        for &j in list {
            if lanes[j].is_nan() { return None; }
            s += lanes[j];
        }
        Some(s)
    };
    for phase in 0..2 {
        // phase 1: the map's counts alone; phase 2: a road still without one counts as its class's default lanes
        // where it feeds or takes another (a merge with a ramp adds a lane, a split takes one) - after the map's own
        // counts went as far as they go
        if phase == 1 {
            for i in 0..n {
                if lanes[i].is_nan() && !at[i][3].is_nan() {
                    deflt[i] = true;
                    lanes[i] = at[i][3];
                }
            }
        }
        for _ in 0..16 {
            let mut changed = false;
            for i in 0..n {
                if !lanes[i].is_nan() && !(phase == 1 && deflt[i]) {
                    continue;
                }
                let (t, h) = (tail[i], head[i]);
                let mut got = None;
                if !blocked[t] && outs[t].len() == 1 {
                    got = sum(&ins[t], &lanes);
                }
                if got.is_none() && !blocked[h] && ins[h].len() == 1 {
                    got = sum(&outs[h], &lanes);
                }
                // a branch of a split (what arrives less the other branches) or of a merge (what leaves less the
                // other roads arriving), at least one lane
                let others = |list: &Vec<usize>| -> Vec<usize> { list.iter().copied().filter(|&j| j != i).collect() };
                if got.is_none() && !blocked[t] && outs[t].len() > 1 {
                    if let (Some(a), Some(b)) = (sum(&ins[t], &lanes), sum(&others(&outs[t]), &lanes)) {
                        if a - b >= 1.0 { got = Some(a - b); }
                    }
                }
                if got.is_none() && !blocked[h] && ins[h].len() > 1 {
                    if let (Some(a), Some(b)) = (sum(&outs[h], &lanes), sum(&others(&ins[h]), &lanes)) {
                        if a - b >= 1.0 { got = Some(a - b); }
                    }
                }
                if phase == 1 && got.map_or(false, |v| v <= at[i][3]) {
                    got = None;                 // (on defaults only a merge's added lane: never narrower than its own)
                }
                if let Some(v) = got {
                    let v = v.min(max_lanes);
                    if lanes[i].is_nan() || (v - lanes[i]).abs() > 0.5 {
                        changed = true;
                    }
                    lanes[i] = v;
                    if phase == 1 {
                        found_default[i] = true;
                    }
                }
            }
            if !changed {
                break;
            }
        }
    }
    // only what was found: the map's own counts and the ramps' one lane stay the caller's
    (0..n).map(|i| if known0[i] || (deflt[i] && !found_default[i]) { f64::NAN } else { lanes[i] }).collect()
}

/// Lane line shifts at merges and splits (server roads.py lane_shifts): where a one-way road takes a ramp and goes on
/// wider (a merge) or leaves one and goes on narrower (a split), the wider piece's lines start shifted toward the ramp
/// so they go on from the narrower piece's (the added lane opens on the ramp's side). lonlat (P, 2), off (n + 1);
/// attrs (n, 4): one-way direction along the geometry (+1 / -1; 0: not counted), drawn width (m), ramp (1), class
/// code; params: m of easing per m of shift, shortest, longest, share of the road, least shift. Returns (T, 5): road,
/// lon, lat (the joint), shift (m, + to the left of its geometry), length (m).
#[pyfunction]
pub fn lane_shifts<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                        attrs: PyReadonlyArray2<'py, f64>, params: PyReadonlyArray1<'py, f64>)
                        -> Bound<'py, PyArray2<f64>> {
    let (ll, off, at, p) = (lonlat.as_array(), off.as_array(), attrs.as_array(), params.as_array());
    let (m_per_m, min_l, max_l, share, min_shift) = (p[0], p[1], p[2], p[3], p[4]);
    let n = off.len().saturating_sub(1);
    let rows = py.detach(|| {
        let mut rows: Vec<[f64; 5]> = Vec::new();
        let ok: Vec<bool> = (0..n).map(|i| (off[i + 1] - off[i]) >= 2 && at[[i, 0]] != 0.0).collect();
        if !ok.iter().any(|&b| b) {
            return rows;
        }
        let (mut lat_sum, mut cnt) = (0.0, 0.0);
        for i in 0..n {
            if ok[i] {
                lat_sum += ll[[off[i] as usize, 1]];
                cnt += 1.0;
            }
        }
        let (mx, my) = (111320.0 * (lat_sum / cnt).to_radians().dcos(), 110540.0);
        let key = |k: usize| ((ll[[k, 0]] * 1e6).round() as i64, (ll[[k, 1]] * 1e6).round() as i64);
        // per node: roads arriving and leaving (by traffic), with the end of each at it
        let mut ins: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
        let mut outs: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
        let mut node = vec![((0i64, 0i64), (0i64, 0i64)); n];         // (tail, head)
        for i in 0..n {
            if !ok[i] {
                continue;
            }
            let (a, b) = (key(off[i] as usize), key(off[i + 1] as usize - 1));
            let (t, h) = if at[[i, 0]] > 0.0 { (a, b) } else { (b, a) };
            node[i] = (t, h);
            outs.entry(t).or_default().push(i);
            ins.entry(h).or_default().push(i);
        }
        // unit direction of travel of road i at node end (start: leaving the node; end: arriving at it), and its length
        let travel = |i: usize, at_tail: bool| -> (Option<[f64; 2]>, f64) {
            let (a, b) = (off[i] as usize, off[i + 1] as usize);
            let fwd = at[[i, 0]] > 0.0;
            // the geometry end at the node: tail = start when fwd
            let from_start = at_tail == fwd;
            let idx = |k: usize| if from_start { a + k } else { b - 1 - k };
            let (x0, y0) = (ll[[idx(0), 0]], ll[[idx(0), 1]]);
            let (mut s, mut dir, mut px, mut py_) = (0.0, None, 0.0, 0.0);
            for k in 1..(b - a) {
                let (x, y) = ((ll[[idx(k), 0]] - x0) * mx, (ll[[idx(k), 1]] - y0) * my);
                s += (x - px).dhypot(y - py_);
                px = x;
                py_ = y;
                if dir.is_none() && s >= 15.0 {
                    dir = Some([x, y]);
                }
            }
            let v = dir.unwrap_or([px, py_]);
            let l = v[0].dhypot(v[1]);
            if l < 1e-6 {
                return (None, s);
            }
            // away from the node; travel direction: away at the tail, toward it at the head
            let u = [v[0] / l, v[1] / l];
            (Some(if at_tail { u } else { [-u[0], -u[1]] }), s)
        };
        // geometry's left at the node end, as +1 / -1 against the travel direction
        let geom_sign = |i: usize| if at[[i, 0]] > 0.0 { 1.0 } else { -1.0 };
        // the offset (m, travel-left of `di`) from the node of road r's point `dist` m along it from its end at the
        // node, and every (arc length, that offset) on the way
        let lateral = |r: usize, r_at_tail: bool, di: [f64; 2]| -> Vec<(f64, f64)> {
            let (a, b) = (off[r] as usize, off[r + 1] as usize);
            let from_start = r_at_tail == (at[[r, 0]] > 0.0);
            let idx = |k: usize| if from_start { a + k } else { b - 1 - k };
            let (x0, y0) = (ll[[idx(0), 0]], ll[[idx(0), 1]]);
            let (mut s, mut px, mut py_) = (0.0, 0.0, 0.0);
            let mut out = vec![(0.0, 0.0)];
            for k in 1..(b - a) {
                let (x, y) = ((ll[[idx(k), 0]] - x0) * mx, (ll[[idx(k), 1]] - y0) * my);
                s += (x - px).dhypot(y - py_);
                px = x;
                py_ = y;
                out.push((s, di[0] * y - di[1] * x));
            }
            out
        };
        for i in 0..n {
            if !ok[i] {
                continue;
            }
            for at_tail in [true, false] {
                let (t, h) = node[i];
                let nd = if at_tail { t } else { h };
                // a merge into i at its tail: i the only road leaving, one same-class road and ramp(s) arriving;
                // a split off i at its head: i the only road arriving, one same-class road and ramp(s) leaving
                let (alone, others) = if at_tail { (outs.get(&nd), ins.get(&nd)) } else { (ins.get(&nd), outs.get(&nd)) };
                let (Some(alone), Some(others)) = (alone, others) else { continue };
                if alone.len() != 1 || others.len() < 2 {
                    continue;
                }
                let mains: Vec<usize> = others.iter().copied()
                    .filter(|&j| at[[j, 2]] < 0.5 && at[[j, 3]] == at[[i, 3]]).collect();
                let ramps: Vec<usize> = others.iter().copied().filter(|&j| at[[j, 2]] > 0.5).collect();
                if mains.len() != 1 {
                    // no one road going on (two ramps joining into a new road, a road splitting into two): the others
                    // side by side across i, outermost edges flush with i's, each easing back to its own line where
                    // its geometry has moved that far aside (2026-10-31, I-95 by the Middle Branch: two ramps
                    // merging, both centred on the new road's line, its corners 2.6 m past either)
                    let (Some(di), _) = travel(i, at_tail) else { continue };
                    let wi = at[[i, 1]];
                    // each other road: its width, its geometry's offsets aside and where it lies across i (its offset
                    // 60 m on, or at its end); all near i's direction
                    let mut lay: Vec<(f64, usize, f64, Vec<(f64, f64)>)> = Vec::new();
                    let mut fits = true;
                    for &r in others.iter() {
                        let (Some(dr), _) = travel(r, !at_tail) else { fits = false; break };
                        if di[0] * dr[0] + di[1] * dr[1] < (40.0f64).to_radians().dcos() {
                            fits = false;       // (a road meeting at an angle: a junction, not a merge / split)
                            break;
                        }
                        let prof = lateral(r, !at_tail, di);
                        let k = prof.iter().position(|q| q.0 >= 60.0).unwrap_or(prof.len() - 1);
                        lay.push((prof[k].1, r, at[[r, 1]], prof));
                    }
                    if !fits || lay.len() < 2 || wi <= 0.0 {
                        continue;
                    }
                    lay.sort_by(|p, q| q.0.partial_cmp(&p.0).unwrap_or(std::cmp::Ordering::Equal));    // left first
                    let total: f64 = lay.iter().map(|q| q.2).sum();
                    let mut before = 0.0;
                    for (_, r, w, prof) in lay.iter() {
                        let frac = if total - w > 1e-6 { before / (total - w) } else { 0.5 };
                        before += w;
                        // its centre across i (+ left of i's travel): flush left for the first, right for the last
                        let c = wi / 2.0 - w / 2.0 - (wi - w) * frac;
                        if c.abs() < min_shift {
                            continue;
                        }
                        // eased away where its own line has moved that far aside, at most max_l / share of it
                        let reach = prof.iter().find(|q| q.1 * c.signum() >= c.abs()).map(|q| q.0).unwrap_or(max_l);
                        let length = prof.last().map(|q| q.0).unwrap_or(0.0);
                        let span = reach.clamp(min_l, max_l).min(share * length);
                        let shift = c * geom_sign(*r);
                        let v = if (!at_tail) == (at[[*r, 0]] > 0.0) { off[*r] as usize } else { off[*r + 1] as usize - 1 };
                        rows.push([*r as f64, ll[[v, 0]], ll[[v, 1]], shift, span]);
                    }
                    continue;
                }
                if at[[i, 2]] > 0.5 || ramps.is_empty() || mains.len() + ramps.len() != others.len() {
                    continue;                   // (ramps keep their lines)
                }
                let j = mains[0];
                let dw = at[[i, 1]] - at[[j, 1]];
                if dw / 2.0 < min_shift {
                    continue;                   // (not wider than the road it goes on from)
                }
                let (Some(di), length) = travel(i, at_tail) else { continue };
                // the ramps' side of i: + left of its travel direction
                let mut side = 0.0;
                for &r in &ramps {
                    let (Some(dr), _) = travel(r, !at_tail) else { continue };
                    // the ramp's travel direction, at the node; where it comes from / goes to relative to i
                    let v = if at_tail { [-dr[0], -dr[1]] } else { dr };
                    side += di[0] * v[1] - di[1] * v[0];
                }
                if side.abs() < 1e-3 {
                    continue;
                }
                let toward = side.signum() * geom_sign(i);      // (+ left of i's geometry)
                let shift = toward * dw / 2.0;
                let span = (dw / 2.0 * m_per_m).clamp(min_l, max_l).min(share * length);
                let v = if (at_tail && at[[i, 0]] > 0.0) || (!at_tail && at[[i, 0]] < 0.0) { off[i] as usize }
                        else { off[i + 1] as usize - 1 };
                rows.push([i as f64, ll[[v, 0]], ll[[v, 1]], shift, span]);
            }
        }
        rows
    });
    let flat: Vec<f64> = rows.iter().flatten().copied().collect();
    numpy::ndarray::Array2::from_shape_vec((rows.len(), 5), flat).unwrap().into_pyarray(py)
}
