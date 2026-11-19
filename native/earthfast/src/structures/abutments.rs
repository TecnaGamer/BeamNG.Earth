//! structure_abutments (earthfast 493, road revamp RS step S2b part 2a): ONE abutment height per structure end.
//!
//! The joint height solve (heights.rs) brings twins together along their decks but keeps every abutment where the
//! plans put it (each deck's own approach road's graded end): Ottawa's 417 twins stayed 0.6-0.7 m apart at their ends,
//! E6 Vist's 0.43 m. Here the decks that are one structure side by side (the grouping's BESIDE / BESIDE_OUTLINE /
//! OVERLAP_ALONG edges, found height-free as the chain closure finds them: structure_touch's CLOSURE mode, no
//! outline) share one abutment height at each common end, which grading's plans then give every approach road ending
//! there (grading._pin_struct_ends, BNE_DECK_STRUCT=abut): the decks' pins agree because the plans do.
//!
//! - A member duplicating another (one way kept twice under two part keys: groups.rs duplicates) is left out; its ends
//!   take its original's nearer end's answer.
//! - An ABUTMENT is a member's end with no other member's end within FREE_M (a joint: the deck weld's) and not inside
//!   another same-level member's carriageway (an end on another deck: END_ON's).
//! - Two abutments are one END GROUP when their members are twins (an edge above) and the ends face the same way
//!   (outward directions within END_TURN_DEG, each read END_DIR_M back along its member), lie within STAGGER_M of
//!   each other along that way and within both half widths plus OUTLINE_GAP_M across it. Union-find over the ends in
//!   the members' order (the caller's canonical one): the groups are the same sets whatever the order.
//! - Per end group of 2..=GROUP_MAX ends: J = max(mean of the members' ground estimates h, the highest clearance need),
//!   rounded to 1 mm, the mean summed in the ends' order (member, end). A group whose estimates spread over SPREAD_MAX_M
//!   (the grouping's BESIDE |dz| limit: two structures, not one) or with an unknown estimate gets none (status).
//!
//! No FMA, no libm, no hash maps: sqrt (correctly rounded) and + - * / only, in a fixed order.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::groups::{groups_core, member_duplicates, members, Member, BESIDE, BESIDE_OUTLINE, BESIDE_DZ, OUTLINE_GAP_M, OVERLAP_ALONG,
                    TWIN_TOUCH_M};

pub const FREE_M: f64 = 1.5;            // m: another member's end this near: a joint, not an abutment
pub const END_DIR_M: f64 = 10.0;        // m back along a member its end's outward direction is read over
pub const END_TURN_COS: f64 = 0.8660254037844387;   // cos(30 degrees): END_TURN_DEG, two ends facing one way
pub const STAGGER_M: f64 = 15.0;        // m two twins' abutments may lie apart along their way
pub const SPREAD_MAX_M: f64 = BESIDE_DZ;    // m: the ground estimates of one end group farther apart: none
pub const GROUP_MAX: usize = 8;         // ends in one end group at most (more: none, status)

pub const ST_ALONE: i64 = 0;            // an abutment with no twin's end beside it
pub const ST_SHARED: i64 = 1;           // one height shared
pub const ST_SPREAD: i64 = 2;           // the estimates spread over SPREAD_MAX_M
pub const ST_MANY: i64 = 3;             // over GROUP_MAX ends
pub const ST_UNKNOWN: i64 = 4;          // an estimate unknown (NaN)
pub const ST_JOINT: i64 = 5;            // not an abutment (a joint, an end on another deck), or a member too short

fn dist2(a: [f64; 2], b: [f64; 2]) -> f64 {
    (a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1])
}

/// Distance from p to member m's line.
fn line_dist(m: &Member, p: [f64; 2]) -> f64 {
    let mut best = f64::INFINITY;
    for w in m.xy.windows(2) {
        let (a, b) = (w[0], w[1]);
        let ab = [b[0] - a[0], b[1] - a[1]];
        let l2 = ab[0] * ab[0] + ab[1] * ab[1];
        let t = if l2 > 0.0 { (((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2).clamp(0.0, 1.0) } else { 0.0 };
        let q = [a[0] + t * ab[0], a[1] + t * ab[1]];
        best = best.min(dist2(p, q));
    }
    best.sqrt()
}

/// The point END_DIR_M back from end e (0: first, 1: last) along m, and the end's unit outward direction.
fn outward(m: &Member, e: usize) -> Option<[f64; 2]> {
    let n = m.xy.len();
    if n < 2 {
        return None;
    }
    let len = m.s[n - 1];
    let (p, back) = if e == 0 { (m.xy[0], END_DIR_M.min(len)) } else { (m.xy[n - 1], (len - END_DIR_M).max(0.0)) };
    // (the point at arc `back`)
    let mut k = 0;
    while k + 2 < n && m.s[k + 1] < back {
        k += 1;
    }
    let seg = m.s[k + 1] - m.s[k];
    let t = if seg > 0.0 { ((back - m.s[k]) / seg).clamp(0.0, 1.0) } else { 0.0 };
    let q = [m.xy[k][0] + t * (m.xy[k + 1][0] - m.xy[k][0]), m.xy[k][1] + t * (m.xy[k + 1][1] - m.xy[k][1])];
    let u = [p[0] - q[0], p[1] - q[1]];
    let l = (u[0] * u[0] + u[1] * u[1]).sqrt();
    if l < 1e-6 {
        return None;
    }
    Some([u[0] / l, u[1] / l])
}

fn find(par: &mut [usize], mut x: usize) -> usize {
    while par[x] != x {
        par[x] = par[par[x]];
        x = par[x];
    }
    x
}

pub struct AbutOut {
    pub group: Vec<i64>,        // per end (2 per member): the end group's first end (-1: not an abutment)
    pub height: Vec<f64>,       // per end: the shared height (NaN: none)
    pub status: Vec<i64>,       // per end: ST_*
}

/// The end groups of members ms and their heights from h / need (per end: 2 * member + e).
pub fn abutments_core(ms: &[Member], h: &[f64], need: &[f64]) -> AbutOut {
    let n = ms.len();
    let ne = 2 * n;
    let pt = |k: usize| -> Option<[f64; 2]> {
        let m = &ms[k / 2];
        if m.xy.len() < 2 { None } else if k % 2 == 0 { Some(m.xy[0]) } else { Some(m.xy[m.xy.len() - 1]) }
    };
    // (one way kept twice under two part keys: the copy left out, its ends given its original's answer below)
    let dup = member_duplicates(ms);
    // abutments
    let mut free = vec![false; ne];
    let mut dir = vec![[0.0f64; 2]; ne];
    for k in 0..ne {
        let (i, e) = (k / 2, k % 2);
        if dup[i] != i {
            continue;
        }
        let (Some(p), Some(u)) = (pt(k), outward(&ms[i], e)) else { continue };
        let mut ok = true;
        for j in 0..n {
            if j == i || ms[j].xy.len() < 2 || dup[j] != j {
                continue;
            }
            let a = ms[j].xy[0];
            let b = ms[j].xy[ms[j].xy.len() - 1];
            if dist2(p, a) <= FREE_M * FREE_M || dist2(p, b) <= FREE_M * FREE_M {
                ok = false;
                break;
            }
            let same = ms[j].kind == ms[i].kind && (ms[j].level == ms[i].level
                || !(ms[j].level.is_finite() && ms[i].level.is_finite()));
            if same && line_dist(&ms[j], p) < ms[j].half - TWIN_TOUCH_M {
                ok = false;
                break;
            }
        }
        free[k] = ok;
        dir[k] = u;
    }
    // twins (height-free: every height 0; no outline, so no BESIDE_OUTLINE)
    let flat: Vec<Member> = ms.iter().map(|m| Member { xy: m.xy.clone(), z: vec![0.0; m.z.len()], s: m.s.clone(),
        half: m.half, kind: m.kind, level: m.level, fid: m.fid, outside: false }).collect();
    let out = groups_core(&flat, &[], true);
    let mut par: Vec<usize> = (0..ne).collect();
    for &(a, b, kind, _u, _dz) in &out.edges {
        if (kind != BESIDE && kind != BESIDE_OUTLINE && kind != OVERLAP_ALONG) || dup[a] != a || dup[b] != b {
            continue;
        }
        for ea in 0..2 {
            for eb in 0..2 {
                let (ka, kb) = (2 * a + ea, 2 * b + eb);
                if !free[ka] || !free[kb] {
                    continue;
                }
                let (ua, ub) = (dir[ka], dir[kb]);
                if ua[0] * ub[0] + ua[1] * ub[1] < END_TURN_COS {
                    continue;
                }
                let mm = [ua[0] + ub[0], ua[1] + ub[1]];
                let l = (mm[0] * mm[0] + mm[1] * mm[1]).sqrt();
                let mm = [mm[0] / l, mm[1] / l];
                let (pa, pb) = (pt(ka).unwrap(), pt(kb).unwrap());
                let d = [pb[0] - pa[0], pb[1] - pa[1]];
                let along = (d[0] * mm[0] + d[1] * mm[1]).abs();
                let across = (d[0] * mm[1] - d[1] * mm[0]).abs();
                if along <= STAGGER_M && across <= ms[a].half + ms[b].half + OUTLINE_GAP_M {
                    let (ra, rb) = (find(&mut par, ka), find(&mut par, kb));
                    if ra != rb {
                        par[ra.max(rb)] = ra.min(rb);
                    }
                }
            }
        }
    }
    let mut res = AbutOut { group: vec![-1; ne], height: vec![f64::NAN; ne], status: vec![ST_JOINT; ne] };
    let mut members_of: Vec<Vec<usize>> = vec![Vec::new(); ne];
    for k in 0..ne {
        if free[k] {
            let r = find(&mut par, k);
            members_of[r].push(k);
        }
    }
    for r in 0..ne {
        let ks = &members_of[r];
        if ks.is_empty() {
            continue;
        }
        // (ks ascending: pushed in end order; the root is the smallest end)
        let (st, j) = if ks.len() < 2 {
            (ST_ALONE, f64::NAN)
        } else if ks.len() > GROUP_MAX {
            (ST_MANY, f64::NAN)
        } else if ks.iter().any(|&k| !h.get(k).is_some_and(|v| v.is_finite())) {
            (ST_UNKNOWN, f64::NAN)
        } else {
            let (mut lo, mut hi, mut sum) = (f64::INFINITY, f64::NEG_INFINITY, 0.0f64);
            let mut top = f64::NEG_INFINITY;
            for &k in ks {
                lo = lo.min(h[k]);
                hi = hi.max(h[k]);
                sum += h[k];
                if let Some(&v) = need.get(k) {
                    if v.is_finite() {
                        top = top.max(v);
                    }
                }
            }
            if hi - lo > SPREAD_MAX_M {
                (ST_SPREAD, f64::NAN)
            } else {
                let j = (sum / ks.len() as f64).max(top);
                (ST_SHARED, (j * 1000.0).round() / 1000.0)
            }
        };
        for &k in ks {
            res.group[k] = ks[0] as i64;
            res.height[k] = j;
            res.status[k] = st;
        }
    }
    // a copy's ends: its original's nearer end's
    for i in 0..n {
        if dup[i] == i {
            continue;
        }
        for e in 0..2 {
            let (Some(p), Some(a), Some(b)) = (pt(2 * i + e), pt(2 * dup[i]), pt(2 * dup[i] + 1)) else { continue };
            let o = if dist2(p, a) <= dist2(p, b) { 2 * dup[i] } else { 2 * dup[i] + 1 };
            res.group[2 * i + e] = res.group[o];
            res.height[2 * i + e] = res.height[o];
            res.status[2 * i + e] = res.status[o];
        }
    }
    res
}

/// grading._struct_end (BNE_DECK_STRUCT=abut): the end groups of the road decks round a bridge end and their shared
/// heights. pts (P, 2) x y of every member's line, member k rows off[k]..off[k + 1]; half, kind, level, fid as
/// structure_touch's; h, need (2 per member: its first end, its last end) the ground estimate of each end and its
/// clearance need (NaN: none). Returns (group, height, status) per end, as AbutOut.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn structure_abutments<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                                half: PyReadonlyArray1<'py, f64>, kind: PyReadonlyArray1<'py, i64>,
                                level: PyReadonlyArray1<'py, f64>, fid: PyReadonlyArray1<'py, i64>,
                                h: PyReadonlyArray1<'py, f64>, need: PyReadonlyArray1<'py, f64>)
                                -> PyResult<(Vec<i64>, Vec<f64>, Vec<i64>)> {
    let ms = members(&pts, &off, &half, &kind, &level, &fid, None, 2)?;
    let (h, need) = (h.as_array().to_vec(), need.as_array().to_vec());
    if h.len() != 2 * ms.len() || need.len() != 2 * ms.len() {
        return Err(PyValueError::new_err("structure_abutments: h / need must hold 2 values per member"));
    }
    let out = py.detach(|| abutments_core(&ms, &h, &need));
    Ok((out.group, out.height, out.status))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(x0: f64, y0: f64, x1: f64, y1: f64, n: usize) -> Vec<[f64; 3]> {
        (0..=n).map(|k| {
            let t = k as f64 / n as f64;
            [x0 + t * (x1 - x0), y0 + t * (y1 - y0), 0.0]
        }).collect()
    }

    fn mem(pts: &[[f64; 3]], half: f64, fid: i64) -> Member {
        Member::new(pts, half, 0, 1.0, fid, false)
    }

    #[test]
    fn twins_share_their_ends() {
        // two carriageways 60 m long, 9 m apart centre to centre (gap 1 m), one drawn the other way
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(62.0, 9.0, 2.0, 9.0, 6), 4.0, 1);
        let h = [10.0, 11.0, 11.6, 10.3];      // a.first, a.last, b.first (x 62), b.last (x 2)
        let need = [f64::NAN; 4];
        let out = abutments_core(&[a, b], &h, &need);
        assert_eq!(out.status, vec![ST_SHARED; 4]);
        // a.first with b.last (x 0 / x 2), a.last with b.first (x 60 / x 62)
        assert_eq!(out.group, vec![0, 1, 1, 0]);
        assert_eq!(out.height[0], 10.15);
        assert_eq!(out.height[3], 10.15);
        assert_eq!(out.height[1], 11.3);
        assert_eq!(out.height[2], 11.3);
    }

    #[test]
    fn a_need_lifts_the_whole_end() {
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(0.0, 9.0, 60.0, 9.0, 6), 4.0, 1);
        let out = abutments_core(&[a, b], &[10.0, 11.0, 10.2, 11.0], &[f64::NAN, f64::NAN, 12.5, f64::NAN]);
        assert_eq!(out.height[0], 12.5);
        assert_eq!(out.height[2], 12.5);
        assert_eq!(out.height[1], 11.0);
    }

    #[test]
    fn spread_staggered_joints_and_order() {
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(0.0, 9.0, 60.0, 9.0, 6), 4.0, 1);
        // estimates 1.5 m apart at the first ends: two structures there
        let out = abutments_core(&[a, b], &[10.0, 11.0, 11.5, 11.0], &[f64::NAN; 4]);
        assert_eq!(out.status[0], ST_SPREAD);
        assert_eq!(out.status[1], ST_SHARED);
        // staggered 20 m: alone
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(20.0, 9.0, 80.0, 9.0, 6), 4.0, 1);
        let out = abutments_core(&[a, b], &[10.0; 4], &[f64::NAN; 4]);
        assert_eq!(out.status, vec![ST_ALONE; 4]);
        // a deck going on from a's end: a joint there, b alone at that end
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(0.0, 9.0, 60.0, 9.0, 6), 4.0, 1);
        let c = mem(&line(60.0, 0.0, 90.0, 0.0, 3), 4.0, 2);
        let out = abutments_core(&[a, b, c], &[10.0; 6], &[f64::NAN; 6]);
        assert_eq!(out.status[1], ST_JOINT);
        assert_eq!(out.status[3], ST_ALONE);
        assert_eq!(out.status[0], ST_SHARED);
        // the same sets and heights in another member order
        let h = [10.0, 11.0, 10.4, 11.2];
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(0.0, 9.0, 60.0, 9.0, 6), 4.0, 1);
        let o1 = abutments_core(&[a, b], &h, &[f64::NAN; 4]);
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(0.0, 9.0, 60.0, 9.0, 6), 4.0, 1);
        let o2 = abutments_core(&[b, a], &[h[2], h[3], h[0], h[1]], &[f64::NAN; 4]);
        assert_eq!(o1.height, vec![o2.height[2], o2.height[3], o2.height[0], o2.height[1]]);
        // a's way kept twice (two part keys, one drawn the other way): the copy takes a's answers, nothing a joint
        let a = mem(&line(0.0, 0.0, 60.0, 0.0, 6), 4.0, 0);
        let b = mem(&line(0.0, 9.0, 60.0, 9.0, 6), 4.0, 1);
        let c = mem(&line(60.0, 0.0, 0.0, 0.0, 6), 4.0, 0);
        let out = abutments_core(&[a, b, c], &[10.0, 11.0, 10.4, 11.2, f64::NAN, f64::NAN], &[f64::NAN; 6]);
        assert_eq!(out.status, vec![ST_SHARED; 6]);
        assert_eq!((out.height[0], out.height[5]), (10.2, 10.2));
        assert_eq!((out.height[1], out.height[4]), (11.1, 11.1));
    }
}
