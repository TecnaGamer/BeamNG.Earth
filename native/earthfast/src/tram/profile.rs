//! The height a tram's track is laid at (plan 046, stage A): one profile for the ground tram's grading plan, from the DEM
//! under it and the roads beside it.
//!
//! The track is draped on the graded ground, and the grader knew nothing of it: near a road the ground under it was the
//! road's embankment (a 2:1 slope off the road's band) or a side street's junction bank, a wall of 0.5-0.7 m in two
//! metres along the axis (Vinnytsia, Khmelnytske shose, 8 % of the steps over 12 cm). A tram runs on the road's own
//! formation, so its bed is held within a band of the nearby roads' heights, a few centimetres on the carriageway and
//! growing by ROAD_GRADE per metre away from it, then limited to TRAM_GRADE, and lets go of the roads (the DEM's trend
//! alone, smoothed) where none is near. Each step is a local operation on a window of points (the smoothing's radius, the
//! band, the slope limit's reach is the height it has to run out over the grade), so the profile is the same in every
//! window that holds a stretch of track with margin. Parts that meet (a way's joint, a switch) are brought to one height
//! there and the difference is run out along them. Pure given its inputs.
use super::lines::P2;
use crate::dm::Dm;

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Points either side the DEM's heights are averaged over, twice (at ~2 m a point: the track rides no bump of the ground).
pub const DEM_SMOOTH_R: usize = 6;
/// m: roads whose edge is this much farther than the nearest one's do not count at a point.
pub const ROAD_TIE_M: f64 = 1.0;
/// m from a carriageway's edge a road pulls the bed to its height.
pub const ATTRACT_M: f64 = 8.0;
/// The bed's allowed distance from a road's height: this on the carriageway (the track in the street is the road's) ...
pub const BAND_BASE_M: f64 = 0.03;
/// ... plus this per metre from the carriageway's edge (a reserved track beside a road lies on its formation).
pub const ROAD_GRADE: f64 = 0.05;
/// The steepest the bed runs along the track (a tram's 50 per mille: 10 cm in the 2 m of a step).
pub const TRAM_GRADE: f64 = 0.05;
/// m the ground is raised under a road in the physics (grading.ROAD_LIFT): the bed beside a road is held to the road's
/// surface, not to its plan's datum.
pub const ROAD_LIFT_M: f64 = 0.04;
/// The grading plan's flat band either side of the tram's line, and the half of it that never gives way (the track's bed).
pub const PLAN_HALF_M: f64 = 1.6;
pub const PLAN_ROAD_HALF_M: f64 = 1.3;
/// m: two part ends this near are one joint; an end this near another part's line (not near its ends) is a switch.
pub const JOINT_M: f64 = 1.5;
/// m along a part the difference at a joint is run out over (smoothly, to nothing).
pub const JOINT_REACH_M: f64 = 40.0;
/// m from a carriageway's edge the bed is held to the road's surface whatever the grade asks (a crossing at grade: flush).
pub const HARD_EDGE_M: f64 = 0.5;
/// m: parallel tracks (a yard's, a double track's) this near each other share a bed, the nearer the closer.
pub const COUPLE_M: f64 = 6.0;
/// Rounds of that sharing.
pub const COUPLE_ROUNDS: usize = 3;
/// m the bed may stand over the terrain's own smooth surface where no street leads it (a tram's track lies on the ground:
/// where the grade limit would raise it more, it is cut into the ground instead).
pub const FILL_MAX_M: f64 = 0.15;
/// Points either side of the last rounding of the profile's kinks.
pub const ROUND_R: usize = 2;

/// The roads' lines with their heights and carriageway half widths (the plans of the cell being graded).
#[derive(Default)]
pub struct Roads {
    pub lines: Vec<(Vec<P2>, Vec<f64>, f64)>,
}

fn arc(xy: &[P2]) -> Vec<f64> {
    let mut s = vec![0.0f64; xy.len()];
    for k in 1..xy.len() {
        s[k] = s[k - 1] + (xy[k][0] - xy[k - 1][0]).dhypot(xy[k][1] - xy[k - 1][1]);
    }
    s
}

/// The running mean over 2r + 1 values, the ends extended by odd reflection (railways._avg).
pub(crate) fn avg(v: &[f64], r: usize) -> Vec<f64> {
    let n = v.len();
    let r = r.min(n.saturating_sub(1));
    if r < 1 {
        return v.to_vec();
    }
    let mut pad = Vec::with_capacity(n + 2 * r);
    for k in (1..=r).rev() {
        pad.push(2.0 * v[0] - v[k]);
    }
    pad.extend_from_slice(v);
    for k in 0..r {
        pad.push(2.0 * v[n - 1] - v[n - 2 - k]);
    }
    let mut c = Vec::with_capacity(pad.len() + 1);
    c.push(0.0f64);
    let mut acc = 0.0f64;
    for &p in &pad {
        acc += p;
        c.push(acc);
    }
    let w = (2 * r + 1) as f64;
    (0..n).map(|i| (c[i + 2 * r + 1] - c[i]) / w).collect()
}

/// The nearest point of segment a-b to p: (distance, t).
fn seg_near(p: P2, a: P2, b: P2) -> (f64, f64) {
    let d = [b[0] - a[0], b[1] - a[1]];
    let l2 = (d[0] * d[0] + d[1] * d[1]).max(1e-12);
    let t = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
    ((p[0] - (a[0] + t * d[0])).dhypot(p[1] - (a[1] + t * d[1])), t)
}

/// |cos| of the angle between the track and a road from which the road runs along the track (a street the tram is on or
/// beside) rather than across it.
pub const PARALLEL_COS: f64 = 0.7;

/// Per point: the roads' height there and the distance to the nearest road's edge; NaN height where no road is within
/// ATTRACT_M. Roads that run along the track (a street the tram is on or beside) lead: where there is one, the roads that
/// cross it (a side street) are left out and follow the track instead (follow_roads); where none runs along it, the
/// nearest roads lead (within ROAD_TIE_M of the nearest one's edge: a junction's roads' mean).
fn road_pull(xy: &[P2], roads: &Roads) -> (Vec<f64>, Vec<f64>) {
    let n = xy.len();
    // (edge distance, road height, runs along the track) per road near the point
    let mut cand: Vec<Vec<(f64, f64, bool)>> = vec![Vec::new(); n];
    let dir = |i: usize| -> P2 {
        let (a, b) = (xy[i.saturating_sub(1)], xy[(i + 1).min(n - 1)]);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let l = dx.dhypot(dy).max(1e-9);
        [dx / l, dy / l]
    };
    for (line, lz, hw) in &roads.lines {
        if line.len() < 2 || lz.len() < line.len() {
            continue;
        }
        let reach = hw + ATTRACT_M;
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for p in line {
            x0 = x0.min(p[0]);
            x1 = x1.max(p[0]);
            y0 = y0.min(p[1]);
            y1 = y1.max(p[1]);
        }
        for (i, p) in xy.iter().enumerate() {
            if p[0] < x0 - reach || p[0] > x1 + reach || p[1] < y0 - reach || p[1] > y1 + reach {
                continue;
            }
            let (mut best, mut bz, mut bk) = (f64::INFINITY, f64::NAN, 0usize);
            for k in 0..line.len() - 1 {
                let (dist, t) = seg_near(*p, line[k], line[k + 1]);
                if dist < best {
                    best = dist;
                    bz = lz[k] + t * (lz[k + 1] - lz[k]);
                    bk = k;
                }
            }
            let edge = (best - hw).max(0.0);
            if edge <= ATTRACT_M && bz.is_finite() {
                let (a, b) = (line[bk], line[bk + 1]);
                let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                let l = dx.dhypot(dy).max(1e-9);
                let td = dir(i);
                let par = ((dx / l) * td[0] + (dy / l) * td[1]).abs() >= PARALLEL_COS;
                cand[i].push((edge, bz + ROAD_LIFT_M, par));
            }
        }
    }
    let (mut zr, mut dmin) = (vec![f64::NAN; n], vec![f64::INFINITY; n]);
    for i in 0..n {
        let any_par = cand[i].iter().any(|c| c.2);
        let pool: Vec<&(f64, f64, bool)> = cand[i].iter().filter(|c| c.2 || !any_par).collect();
        let near = pool.iter().map(|c| c.0).fold(f64::INFINITY, f64::min);
        if !near.is_finite() {
            continue;
        }
        let (mut sum, mut cnt) = (0.0f64, 0.0f64);
        for &&(e, z, _) in &pool {
            if e <= near + ROAD_TIE_M {
                sum += z;
                cnt += 1.0;
            }
        }
        zr[i] = sum / cnt;
        dmin[i] = near;
    }
    (zr, dmin)
}

/// The profile held to a grade g along the track: the mean of the upper envelope (the lowest of every other height raised
/// by g per metre) and the lower one (the highest lowered by g per metre): a step becomes a ramp straddling it.
fn limit_grade(s: &[f64], z: &[f64], g: f64) -> Vec<f64> {
    let n = z.len();
    let (mut up, mut lo) = (z.to_vec(), z.to_vec());
    for i in 1..n {
        let ds = s[i] - s[i - 1];
        up[i] = up[i].min(up[i - 1] + g * ds);
        lo[i] = lo[i].max(lo[i - 1] - g * ds);
    }
    for i in (0..n.saturating_sub(1)).rev() {
        let ds = s[i + 1] - s[i];
        up[i] = up[i].min(up[i + 1] + g * ds);
        lo[i] = lo[i].max(lo[i + 1] - g * ds);
    }
    (0..n).map(|i| 0.5 * (up[i] + lo[i])).collect()
}

/// z held inside the roads' bands (per point: zr +- (BAND_BASE_M + ROAD_GRADE * its distance to the road's edge)).
fn clamp_band(z: &mut [f64], zr: &[f64], dmin: &[f64]) {
    for i in 0..z.len() {
        if zr[i].is_finite() {
            let band = BAND_BASE_M + ROAD_GRADE * dmin[i];
            z[i] = z[i].clamp(zr[i] - band, zr[i] + band);
        }
    }
}

/// A bed before its last clamp: the heights, and the bounds the roads it crosses at grade (HARD_EDGE_M) hold it to, each
/// as an envelope at the grade (a bound reaches TRAM_GRADE per metre either way along the track).
struct Raw {
    s: Vec<f64>,
    z: Vec<f64>,
    zs: Vec<f64>,
    free: Vec<bool>,
    lo: Vec<f64>,
    hi: Vec<f64>,
}

/// The envelope of bounds b (b[i] is a bound at i, NaN none): the lowest of every bound raised by g per metre (up), or the
/// highest lowered by g per metre (the other way), two passes.
fn envelope(s: &[f64], b: &[f64], g: f64, upper: bool) -> Vec<f64> {
    let n = b.len();
    let none = if upper { f64::INFINITY } else { f64::NEG_INFINITY };
    let mut e: Vec<f64> = b.iter().map(|&v| if v.is_finite() { v } else { none }).collect();
    let pick = |a: f64, c: f64| if upper { a.min(c) } else { a.max(c) };
    let step = |v: f64, ds: f64| if upper { v + g * ds } else { v - g * ds };
    for i in 1..n {
        e[i] = pick(e[i], step(e[i - 1], s[i] - s[i - 1]));
    }
    for i in (0..n.saturating_sub(1)).rev() {
        e[i] = pick(e[i], step(e[i + 1], s[i + 1] - s[i]));
    }
    e
}

fn bed_raw(xy: &[P2], zd: &[f64], roads: &Roads) -> Raw {
    let n = xy.len();
    let s = arc(xy);
    let mut z = avg(&avg(zd, DEM_SMOOTH_R), DEM_SMOOTH_R);
    let (zr, dmin) = road_pull(xy, roads);
    clamp_band(&mut z, &zr, &dmin);
    z = limit_grade(&s, &z, TRAM_GRADE);
    clamp_band(&mut z, &zr, &dmin);
    z = avg(&z, ROUND_R);
    let (mut lo, mut hi) = (vec![f64::NAN; n], vec![f64::NAN; n]);
    for i in 0..n {
        if zr[i].is_finite() && dmin[i] <= HARD_EDGE_M {
            let band = BAND_BASE_M + ROAD_GRADE * dmin[i];
            lo[i] = zr[i] - band;
            hi[i] = zr[i] + band;
        }
    }
    let zs = avg(&avg(zd, DEM_SMOOTH_R), DEM_SMOOTH_R);
    let free: Vec<bool> = zr.iter().map(|v| !v.is_finite()).collect();
    Raw { lo: envelope(&s, &lo, TRAM_GRADE, false), hi: envelope(&s, &hi, TRAM_GRADE, true), s, z, zs, free }
}

/// z held at the grade, then inside the bounds the roads it crosses hold it to (a clamp of a profile at the grade into two
/// envelopes at the grade stays at the grade; where the bounds disagree the upper one wins).
fn finish(r: &Raw, z: &[f64], cap: bool) -> Vec<f64> {
    let z = limit_grade(&r.s, z, TRAM_GRADE);
    // (off the streets the bed is the terrain's own smooth surface: the grade limit may not build it up past FILL_MAX_M)
    (0..z.len())
        .map(|i| {
            let v = z[i].max(r.lo[i]).min(r.hi[i]);
            if cap && r.free[i] { v.min(r.zs[i] + FILL_MAX_M) } else { v }
        })
        .collect()
}

/// The bed heights along a tram track xy (n, 2), whose ground (the DEM's, as the planner has it) is zd (n,), laid among roads.
pub fn bed(xy: &[P2], zd: &[f64], roads: &Roads) -> Vec<f64> {
    if xy.len() < 2 || zd.len() != xy.len() {
        return zd.to_vec();
    }
    let r = bed_raw(xy, zd, roads);
    finish(&r, &r.z, true)
}

/// Parallel parts share a bed: each point is drawn towards the mean of the nearest points of the other parts within
/// COUPLE_M, the closer the stronger (a yard's tracks 3 m apart had each its own bed, and the ground a step between them).
fn couple_parallel(parts: &[(Vec<P2>, Vec<f64>)], z: &mut [Vec<f64>]) {
    for _ in 0..COUPLE_ROUNDS {
        let before: Vec<Vec<f64>> = z.to_vec();
        for k in 0..parts.len() {
            for (i, p) in parts[k].0.iter().enumerate() {
                let (mut acc, mut wsum) = (0.0f64, 0.0f64);
                for (k2, (line, _)) in parts.iter().enumerate() {
                    if k2 == k || line.len() < 2 {
                        continue;
                    }
                    let (mut best, mut bz) = (f64::INFINITY, f64::NAN);
                    for j in 0..line.len() - 1 {
                        let (d, t) = seg_near(*p, line[j], line[j + 1]);
                        if d < best {
                            best = d;
                            bz = before[k2][j] + t * (before[k2][j + 1] - before[k2][j]);
                        }
                    }
                    if best < COUPLE_M {
                        let w = 1.0 - best / COUPLE_M;
                        acc += w * bz;
                        wsum += w;
                    }
                }
                if wsum > 0.0 {
                    let w = (wsum / (1.0 + wsum)).min(0.5);
                    z[k][i] = (1.0 - w) * before[k][i] + w * acc / wsum;
                }
            }
        }
    }
}

/// A part's end that meets others: (part, end (0 first / 1 last), the height to meet at).
fn joint_targets(parts: &[(Vec<P2>, Vec<f64>)], z: &[Vec<f64>]) -> Vec<(usize, usize, f64)> {
    let mut ends: Vec<(usize, usize, P2, f64)> = Vec::new();
    for (k, p) in parts.iter().enumerate() {
        if p.0.len() >= 2 {
            let last = p.0.len() - 1;
            ends.push((k, 0, p.0[0], z[k][0]));
            ends.push((k, 1, p.0[last], z[k][last]));
        }
    }
    let mut out = Vec::new();
    for &(k, e, pt, ze) in &ends {
        // other parts' ends within JOINT_M: one joint, the mean of the heights there
        let mut group = vec![ze];
        for &(k2, _e2, pt2, z2) in &ends {
            if k2 != k && (pt[0] - pt2[0]).dhypot(pt[1] - pt2[1]) <= JOINT_M {
                group.push(z2);
            }
        }
        if group.len() > 1 {
            out.push((k, e, group.iter().sum::<f64>() / group.len() as f64));
            continue;
        }
        // the end on another part's line, away from its ends (a switch): that line's height there
        let mut best: Option<(f64, f64)> = None;
        for (k2, (line, _)) in parts.iter().enumerate() {
            if k2 == k || line.len() < 2 {
                continue;
            }
            let s2 = arc(line);
            for j in 0..line.len() - 1 {
                let (dist, t) = seg_near(pt, line[j], line[j + 1]);
                let at = s2[j] + t * (s2[j + 1] - s2[j]);
                if dist <= JOINT_M && at > JOINT_M && at < s2[s2.len() - 1] - JOINT_M && best.map_or(true, |b0| dist < b0.0) {
                    best = Some((dist, z[k2][j] + t * (z[k2][j + 1] - z[k2][j])));
                }
            }
        }
        if let Some((_, zt)) = best {
            out.push((k, e, zt));
        }
    }
    out
}

/// The beds of a cell's ground tram parts (each (xy, zd)): each as `bed`, then the parts that meet brought to one height at
/// their joints (ends within JOINT_M of each other: their mean; an end on another part's line: that line's), the difference
/// run out along the part over JOINT_REACH_M.
pub fn beds(parts: &[(Vec<P2>, Vec<f64>)], roads: &Roads) -> Vec<Vec<f64>> {
    let raws: Vec<Option<Raw>> = parts
        .iter()
        .map(|(xy, zd)| if xy.len() >= 2 && zd.len() == xy.len() { Some(bed_raw(xy, zd, roads)) } else { None })
        .collect();
    let mut z: Vec<Vec<f64>> = parts
        .iter()
        .zip(&raws)
        .map(|((_, zd), r)| match r {
            Some(r) => finish(r, &r.z, true),
            None => zd.clone(),
        })
        .collect();
    couple_parallel(parts, &mut z);
    for (k, e, target) in joint_targets(parts, &z) {
        let s = arc(&parts[k].0);
        let n = s.len();
        let (i0, total) = if e == 0 { (0, 0.0) } else { (n - 1, s[n - 1]) };
        let delta = target - z[k][i0];
        for i in 0..n {
            let x = (1.0 - (s[i] - total).abs() / JOINT_REACH_M).clamp(0.0, 1.0);
            z[k][i] += delta * x * x * (3.0 - 2.0 * x);
        }
    }
    for (k, r) in raws.iter().enumerate() {
        if let Some(r) = r {
            z[k] = finish(r, &z[k], false);     // (no cap: the joints' heights are the parts' means)
        }
    }
    z
}

/// The steepest a side street runs out from a track it crosses at grade, by road class (m per m): a car climbs the ramp
/// to the track and leaves it without a crest.
pub fn follow_grade(class: &str) -> f64 {
    match class.trim_end_matches("_link") {
        "motorway" | "trunk" | "primary" | "secondary" => 0.04,
        "tertiary" | "residential" | "unclassified" | "living_street" => 0.05,
        _ => 0.06,
    }
}
/// m of road either side of a crossing looked at (the ramp reaches farther only where the road is farther off the bed).
pub const FOLLOW_REACH_M: f64 = 80.0;
/// m: a road this far over or under the bed passes it by (a bridge, an underpass), not at grade.
pub const FOLLOW_DZ_M: f64 = 1.5;
/// The crossing's sine from which a road crosses the track rather than runs along it.
pub const FOLLOW_MIN_SIN: f64 = 0.5;

/// Roads (line, heights, class) brought to the beds of the tram tracks (line, bed heights) they cross at grade: where a road
/// meets a bed (its height within FOLLOW_DZ_M of the bed's less the road lift, the lines crossing at over FOLLOW_MIN_SIN),
/// its height is held within follow_grade(class) per metre of the bed's height there (the bed's, less ROAD_LIFT_M, the
/// road's datum) along the road either side: a ramp to the track and from it, never a crest or a sag. A road that crosses
/// none is returned as it is. Returns a height per road vertex.
pub fn follow_roads(roads: &[(Vec<P2>, Vec<f64>, String)], beds: &[(Vec<P2>, Vec<f64>)]) -> Vec<Vec<f64>> {
    let mut out: Vec<Vec<f64>> = roads.iter().map(|r| r.1.clone()).collect();
    for (ri, (line, rz, class)) in roads.iter().enumerate() {
        if line.len() < 2 || rz.len() < line.len() {
            continue;
        }
        let g = follow_grade(class);
        let rs = arc(line);
        let mut anchors: Vec<(f64, f64)> = Vec::new(); // (arc on the road, the road's datum height there)
        for (bl, bz) in beds {
            if bl.len() < 2 || bz.len() < bl.len() {
                continue;
            }
            for k in 0..line.len() - 1 {
                let (a, b) = (line[k], line[k + 1]);
                let (ab, abl) = ([b[0] - a[0], b[1] - a[1]], (b[0] - a[0]).dhypot(b[1] - a[1]).max(1e-9));
                for m in 0..bl.len() - 1 {
                    let (c, d) = (bl[m], bl[m + 1]);
                    let cd = [d[0] - c[0], d[1] - c[1]];
                    let cdl = cd[0].dhypot(cd[1]).max(1e-9);
                    let den = ab[0] * cd[1] - ab[1] * cd[0];
                    if den.abs() < 1e-12 || (den / (abl * cdl)).abs() < FOLLOW_MIN_SIN {
                        continue;
                    }
                    let (ac0, ac1) = (c[0] - a[0], c[1] - a[1]);
                    let t = (ac0 * cd[1] - ac1 * cd[0]) / den;
                    let u = (ac0 * ab[1] - ac1 * ab[0]) / den;
                    if !(0.0..=1.0).contains(&t) || !(0.0..=1.0).contains(&u) {
                        continue;
                    }
                    let zr = rz[k] + t * (rz[k + 1] - rz[k]);
                    let zb = bz[m] + u * (bz[m + 1] - bz[m]) - ROAD_LIFT_M;
                    if (zr - zb).abs() <= FOLLOW_DZ_M {
                        anchors.push((rs[k] + t * (rs[k + 1] - rs[k]), zb));
                    }
                }
            }
        }
        if anchors.is_empty() {
            continue;
        }
        for i in 0..line.len() {
            let (mut lo, mut hi) = (f64::NEG_INFINITY, f64::INFINITY);
            for &(sa, za) in &anchors {
                let d = (rs[i] - sa).abs();
                if d <= FOLLOW_REACH_M {
                    lo = lo.max(za - g * d);
                    hi = hi.min(za + g * d);
                }
            }
            if lo.is_finite() || hi.is_finite() {
                out[ri][i] = if lo > hi { 0.5 * (lo + hi) } else { rz[i].clamp(lo, hi) };
            }
        }
    }
    out
}

/// Per road (end to end: road_pts (m, 2), road_z (m,), road_off (r + 1,), classes (r,)) its heights brought to the tram beds
/// (bed_pts (n, 2), bed_z (n,), bed_off (b + 1,)) it crosses at grade (follow_roads): (m,).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tram_follow<'py>(py: Python<'py>, road_pts: PyReadonlyArray2<'py, f64>, road_z: PyReadonlyArray1<'py, f64>,
                        road_off: PyReadonlyArray1<'py, i64>, classes: Vec<String>, bed_pts: PyReadonlyArray2<'py, f64>,
                        bed_z: PyReadonlyArray1<'py, f64>, bed_off: PyReadonlyArray1<'py, i64>)
                        -> PyResult<Bound<'py, numpy::PyArray1<f64>>> {
    use numpy::IntoPyArray;
    let (rp, rz, ro) = (pts2(&road_pts), road_z.as_array().to_vec(), road_off.as_array().to_vec());
    let (bp, bz, bo) = (pts2(&bed_pts), bed_z.as_array().to_vec(), bed_off.as_array().to_vec());
    if rz.len() != rp.len() || bz.len() != bp.len() || ro.is_empty() || bo.is_empty() || ro.len() != classes.len() + 1
        || ro[ro.len() - 1] as usize != rp.len() || bo[bo.len() - 1] as usize != bp.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_follow: mismatched arrays"));
    }
    let roads: Vec<(Vec<P2>, Vec<f64>, String)> = (0..classes.len()).map(|k| {
        let (a, b) = (ro[k] as usize, ro[k + 1] as usize);
        (rp[a..b].to_vec(), rz[a..b].to_vec(), classes[k].clone())
    }).collect();
    let beds: Vec<(Vec<P2>, Vec<f64>)> = (0..bo.len() - 1).map(|k| {
        let (a, b) = (bo[k] as usize, bo[k + 1] as usize);
        (bp[a..b].to_vec(), bz[a..b].to_vec())
    }).collect();
    let out: Vec<f64> = py.detach(|| follow_roads(&roads, &beds)).into_iter().flatten().collect();
    Ok(numpy::ndarray::Array1::from(out).into_pyarray(py))
}

/// The plan's numbers for the grader's glue: name -> metres.
#[pyfunction]
pub fn tram_bed_consts(py: Python<'_>) -> PyResult<Bound<'_, pyo3::types::PyDict>> {
    let d = pyo3::types::PyDict::new(py);
    for (k, v) in [("PLAN_HALF_M", PLAN_HALF_M), ("PLAN_ROAD_HALF_M", PLAN_ROAD_HALF_M), ("ATTRACT_M", ATTRACT_M),
                   ("BAND_BASE_M", BAND_BASE_M), ("ROAD_GRADE", ROAD_GRADE), ("TRAM_GRADE", TRAM_GRADE),
                   ("JOINT_M", JOINT_M), ("JOINT_REACH_M", JOINT_REACH_M), ("HARD_EDGE_M", HARD_EDGE_M),
                   ("COUPLE_M", COUPLE_M)] {
        d.set_item(k, v)?;
    }
    Ok(d)
}

fn pts2(a: &PyReadonlyArray2<f64>) -> Vec<P2> {
    let v = a.as_array();
    (0..v.nrows()).map(|i| [v[[i, 0]], v[[i, 1]]]).collect()
}

/// The bed heights of a cell's ground tram parts, end to end (xy (n, 2), zd (n,), part_off (p + 1,)), among the roads
/// beside them (road_pts (m, 2), road_z (m,), road_off (r + 1,), road_hw (r,): their carriageways' half widths): (n,).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tram_beds<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, zd: PyReadonlyArray1<'py, f64>,
                      part_off: PyReadonlyArray1<'py, i64>, road_pts: PyReadonlyArray2<'py, f64>,
                      road_z: PyReadonlyArray1<'py, f64>, road_off: PyReadonlyArray1<'py, i64>,
                      road_hw: PyReadonlyArray1<'py, f64>) -> PyResult<Bound<'py, numpy::PyArray1<f64>>> {
    use numpy::IntoPyArray;
    let (p, z, po) = (pts2(&xy), zd.as_array().to_vec(), part_off.as_array().to_vec());
    let (rp, rz, off, hw) = (pts2(&road_pts), road_z.as_array().to_vec(), road_off.as_array().to_vec(), road_hw.as_array().to_vec());
    if z.len() != p.len() || rz.len() != rp.len() || off.len() != hw.len() + 1
        || off.last().copied().unwrap_or(0) as usize > rp.len() || po.is_empty() || po.last().copied().unwrap_or(0) as usize != p.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_beds: mismatched arrays"));
    }
    let roads = Roads {
        lines: (0..hw.len()).map(|r| {
            let (a, b) = (off[r] as usize, off[r + 1] as usize);
            (rp[a..b].to_vec(), rz[a..b].to_vec(), hw[r])
        }).collect(),
    };
    let parts: Vec<(Vec<P2>, Vec<f64>)> = (0..po.len() - 1).map(|k| {
        let (a, b) = (po[k] as usize, po[k + 1] as usize);
        (p[a..b].to_vec(), z[a..b].to_vec())
    }).collect();
    let out: Vec<f64> = py.detach(|| beds(&parts, &roads)).into_iter().flatten().collect();
    Ok(numpy::ndarray::Array1::from(out).into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(n: usize, y: f64) -> Vec<P2> {
        (0..n).map(|i| [2.0 * i as f64, y]).collect()
    }

    fn max_step(z: &[f64]) -> f64 {
        z.windows(2).map(|w| (w[1] - w[0]).abs()).fold(0.0, f64::max)
    }

    #[test]
    fn open_track_follows_the_dem_without_its_bumps_and_steps() {
        let xy = line(200, 0.0);
        // a slope of 2 % with 0.4 m bumps and a 0.7 m wall
        let zd: Vec<f64> = (0..200).map(|i| 0.04 * i as f64 + if i % 7 == 0 { 0.4 } else { 0.0 } + if i > 100 { 0.7 } else { 0.0 }).collect();
        let z = bed(&xy, &zd, &Roads::default());
        assert!(max_step(&z) <= TRAM_GRADE * 2.0 * 2.0 + 1e-9, "no step over twice the tram grade: {}", max_step(&z));
    }

    #[test]
    fn a_track_beside_a_raised_road_lies_on_its_formation() {
        // a road along y = 0, 1.2 m above the DEM, the track 7 m from its edge on a DEM 1.2 m lower
        let xy = line(100, 8.0);
        let road = (line(100, 0.0), vec![10.0; 100], 1.0);
        let zd = vec![8.8; 100];
        let z = bed(&xy, &zd, &Roads { lines: vec![road] });
        let band = BAND_BASE_M + ROAD_GRADE * 7.0;
        assert!(z.iter().all(|&v| v >= 10.0 - band - 1e-9), "within the band of the road's height: {}", z[50]);
    }

    #[test]
    fn a_side_street_crossing_at_grade_is_flush_and_ramped() {
        // the track along x at y = 8 over a road along y = 0 (1.2 m up); a side street along x = 100 (a crossing at
        // grade) down at the DEM: the track meets it flush and runs out to the road's height at TRAM_GRADE
        let xy = line(100, 8.0);
        let main = (line(100, 0.0), vec![10.0; 100], 1.0);
        let side: Vec<P2> = (0..20).map(|k| [100.0, -4.0 + 2.0 * k as f64]).collect();
        let side_z: Vec<f64> = side.iter().map(|p| if p[1] < 4.0 { 10.0 } else { 8.8 }).collect();
        let zd = vec![8.8; 100];
        let z = bed(&xy, &zd, &Roads { lines: vec![main, (side, side_z, 3.0)] });
        assert!(max_step(&z) <= TRAM_GRADE * 2.0 * 2.0 + 1e-9, "{}", max_step(&z));
        // x = 100 is the 50th point: the main road runs along the tram and leads; the side street crossing it follows
        // the tram (follow_roads), so the bed is on the main road's surface there
        assert!(z[50] >= 10.04 - BAND_BASE_M - ROAD_GRADE * 7.0 - 1e-6, "within the main road's band at the crossing: {}", z[50]);
    }

    #[test]
    fn a_side_street_follows_the_tram_up_a_ramp_instead_of_a_crest() {
        // the tram along y = 0 at 10 m; a side street across it, flat at 10 m to y = 0 then falling 20 % (the crest the
        // junction left): after the rule it falls at 4 % (a secondary) from the crossing at most
        let bed = ((0..=50).map(|i| [-50.0 + 2.0 * i as f64, 0.0]).collect::<Vec<P2>>(), vec![10.0 + ROAD_LIFT_M; 51]);
        let side: Vec<P2> = (0..=40).map(|i| [0.0, -40.0 + 2.0 * i as f64]).collect();
        let zs: Vec<f64> = side.iter().map(|p| if p[1] < 0.0 { 10.0 + 0.2 * p[1] } else { 10.0 }).collect();
        let z = &follow_roads(&[(side.clone(), zs.clone(), "secondary".into())], &[bed.clone()])[0];
        let steepest = z.windows(2).map(|w| (w[1] - w[0]).abs() / 2.0).fold(0.0, f64::max);
        assert!(steepest <= 0.04 + 1e-9, "{steepest}");
        assert!((z[20] - 10.0).abs() < 1e-9, "flush at the crossing: {}", z[20]);
        // a road that does not cross it is left alone, and one 6 m under it (a bridge) too
        let far: Vec<P2> = (0..=40).map(|i| [100.0, -40.0 + 2.0 * i as f64]).collect();
        assert_eq!(follow_roads(&[(far, zs.clone(), "primary".into())], &[bed.clone()])[0], zs);
        let under: Vec<f64> = zs.iter().map(|v| v - 6.0).collect();
        assert_eq!(follow_roads(&[(side, under.clone(), "primary".into())], &[bed])[0], under);
    }

    #[test]
    fn a_street_beside_the_tram_leads_and_a_side_street_across_it_does_not() {
        let xy = line(60, 0.0);
        let main = (line(60, 4.0), vec![10.0; 60], 3.0);
        let side: Vec<P2> = (0..20).map(|k| [60.0, -10.0 + 2.0 * k as f64]).collect();
        let z = bed(&xy, &vec![8.0; 60], &Roads { lines: vec![main, (side, vec![8.0; 20], 2.0)] });
        assert!(z[30] > 9.9, "on the main street's level: {}", z[30]);
    }

    #[test]
    fn tracks_side_by_side_share_a_bed() {
        let a = line(60, 0.0);
        let b = line(60, 3.0);
        let parts = vec![(a, vec![10.0; 60]), (b, vec![10.5; 60])];
        let z = beds(&parts, &Roads::default());
        let d = (z[0][30] - z[1][30]).abs();
        assert!(d < 0.3, "a 0.5 m difference 3 m apart came to {d}");
    }

    #[test]
    fn parts_meeting_at_a_joint_or_a_switch_end_at_one_height() {
        // two parts end to end at (100, 0) on different DEMs, and a branch ending on the first's line at (50, 0)
        let a: Vec<P2> = (0..=50).map(|i| [2.0 * i as f64, 0.0]).collect();
        let b: Vec<P2> = (0..=50).map(|i| [100.0 + 2.0 * i as f64, 0.0]).collect();
        let mut c: Vec<P2> = (0..=30).map(|i| [50.0 + 2.0 * i as f64, i as f64 * 0.5]).collect();
        c[0] = [50.0, 0.0];
        let parts = vec![(a, vec![10.0; 51]), (b, vec![10.6; 51]), (c, vec![10.3; 31])];
        let z = beds(&parts, &Roads::default());
        assert!((z[0][50] - z[1][0]).abs() < 1e-9, "end to end: one height {} {}", z[0][50], z[1][0]);
        assert!((z[2][0] - z[0][25]).abs() < 0.05, "the branch's end is on the line's height: {} {}", z[2][0], z[0][25]);
    }
}
