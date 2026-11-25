//! T1 tunnel record (tunnel plan section 4, BNE_ROADSYS_TUNNELS=on with BNE_ROADSYS=surface): one record per tunnel
//! of a carriageway, made in the profile square that owns the carriageway's floor (square.rs), from the same knots:
//! the profile z(s) (prof.rs: one profile through ground and tunnel), the bare ground over the axis g(s) and beside it.
//! Every consumer (terrain holes, road tiles, grading, the lane check) reads it through roads.TunnelProfiles; none
//! re-decides a tunnel.
//!
//! Per mapped tunnel range of a stroke (net.rs SUP_TUNNEL: the map's tag, MAPPED_RANGES):
//! - Rule P (4.1): the tag decides that the road is covered from its mapped end inwards; nothing moves a portal inward.
//!   Outward: where the bare ground just outside a mapped end stands more than D_CUT over the profile (a cutting deeper
//!   than the approach allows: 15 m (49 ft) on roads of a rural design speed, 8 m (26 ft) else), the covered range grows
//!   outward to where it no longer does, at most X_MAX (30 m, 100 ft), along the same stroke only.
//! - Sections per knot (4.2, the part T1 decides): a daylight gap (kind 0: the axis cover under 2 m and both sides'
//!   ground under the floor + 2 m for 40 m or more), else a full tube (kind 2: the cover over the tube's height
//!   + 0.5 m: a bore or a box under ground), else a lid (kind 1: the tube sized under the thin cover, its roof at the
//!   ground; a low arch to today's mesher). Runs under MIN_RUN_M join the run before them. Never "open" for want of
//!   cover: a tagged tunnel with thin ground is a lid, not an open road (I-395, Burnley, Malta).
//! - Mouths: a covered run's end next to the ground or a daylight gap is a portal; a range's end at a stroke end where
//!   another stroke goes on in a tunnel is a joint (no portal).
//! - Holes (4.4): per portal, the reach inward up to where the vault top + 1 m lies under the ground over the axis and
//!   both footprint edges for two knots running (at most HOLE_MAX_M); the terrain blocks cut only squares inside it
//!   (earthfast tunnelrec.rs). Holes nowhere else: the tube runs under intact terrain.
//!
//! IEEE + - * / only, fixed orders, no maps keyed by floats (the crate's rules): the same record on every platform.

pub const TUBE_H: f64 = 6.8;        // m: today's mesher's tube (roadmesh.TUNNEL_H): its outer height over the floor
pub const LID_MARGIN: f64 = 0.5;    // m of cover over the tube for a full tube (else a lid)
pub const X_MAX: f64 = 30.0;        // m (100 ft): a portal's outward snap at most (rule P, calibrated on the references)
pub const D_CUT_RURAL: f64 = 15.0;  // m (49 ft): the deepest approach cut with sloped sides
pub const D_CUT_TOWN: f64 = 8.0;    // m (26 ft): the deepest approach cut between walls
pub const RURAL_KMH: f64 = 60.0;    // a design speed over this: rural cut sides (no town flag in the lane graph yet)
pub const GAP_COVER: f64 = 2.0;     // m: a daylight gap's ground over the floor at most (axis and both sides)
pub const GAP_MIN_M: f64 = 40.0;    // m: a daylight gap's length at least
pub const MIN_RUN_M: f64 = 10.0;    // m: a section shorter than this joins the one before it
pub const HOLE_OVER: f64 = 1.0;     // m: the vault top this far under the ground ends a portal's hole
pub const HOLE_MAX_M: f64 = 60.0;   // m: a portal's hole reaches this far in at most
pub const SIDE_IN: f64 = 1.5;       // m past the carriageway's half width: the tube's footprint edge (+ 0.5 m margin)
pub const SIDE_OFFS: [f64; 3] = [SIDE_IN, 8.0, 16.0];   // m past the half width the side ground is read

/// A covered run of a stroke: [s0, s1] (stations), its kind (0 daylight gap, 1 lid, 2 full tube), whether each end is a
/// portal, its least inner cover (m; the mesher sizes a lid's tube by it), the hole reach in from each portal end (m,
/// 0 at a joint).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Run { pub s0: f64, pub s1: f64, pub kind: u8, pub mouth0: bool, pub mouth1: bool, pub cmin: f64, pub hole0: f64, pub hole1: f64 }

/// A mapped range's ends as the record moved them: (mapped station, built station). Rule P's outward snaps.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Snap { pub mapped: f64, pub built: f64, pub cut: f64 }

/// The inputs of one stroke: knot stations s (h apart from 0, the last at the stroke's length), the profile z, the
/// ground over the axis g, the side ground (left / right, one per SIDE_OFFS, NaN where not read), its mapped tunnel
/// ranges (stations), whether each range end at the stroke's start / end goes on into another stroke's tunnel, and its
/// design speed.
pub struct StrokeIn<'a> {
    pub s: &'a [f64],
    pub z: &'a [f64],
    pub g: &'a [f64],
    pub side_l: &'a [[f64; 3]],
    pub side_r: &'a [[f64; 3]],
    pub ranges: &'a [(f64, f64)],
    pub joined0: bool,
    pub joined1: bool,
    pub kmh: f64,
}

fn cover_at(inp: &StrokeIn, i: usize) -> f64 { inp.g[i] - inp.z[i] }

/// The least of the axis and footprint-edge grounds at knot i (NaN sides ignored).
fn foot_ground(inp: &StrokeIn, i: usize) -> f64 {
    let mut m = inp.g[i];
    for v in [inp.side_l[i][0], inp.side_r[i][0]] { if v.is_finite() && v < m { m = v; } }
    m
}

fn knot_of(s: &[f64], x: f64) -> usize {
    // the last knot at or before x (s ascending)
    let n = s.len();
    if n == 0 { return 0; }
    let k = s.partition_point(|&v| v <= x);
    if k == 0 { 0 } else { (k - 1).min(n - 1) }
}

/// The record of one stroke: its covered runs (in station order) and the snaps of its ranges' ends.
pub fn stroke_record(inp: &StrokeIn) -> (Vec<Run>, Vec<Snap>) {
    let n = inp.s.len();
    let mut runs = Vec::new();
    let mut snaps = Vec::new();
    if n < 2 { return (runs, snaps); }
    let len = inp.s[n - 1];
    let d_cut = if inp.kmh > RURAL_KMH { D_CUT_RURAL } else { D_CUT_TOWN };
    let mut ranges: Vec<(f64, f64)> = inp.ranges.iter().copied().filter(|r| r.1 > r.0).collect();
    ranges.sort_by(|a, b| a.0.total_cmp(&b.0));
    for (ri, &(a0, b0)) in ranges.iter().enumerate() {
        let at_start = a0 <= 1.0;
        let at_end = b0 >= len - 1.0;
        let join0 = at_start && inp.joined0;
        let join1 = at_end && inp.joined1;
        // rule P: outward over the stroke's own knots, never into the previous / next range, never past X_MAX
        let lo_lim = if ri > 0 { ranges[ri - 1].1 } else { 0.0 };
        let hi_lim = if ri + 1 < ranges.len() { ranges[ri + 1].0 } else { len };
        let mut a = a0;
        let mut b = b0;
        if !join0 && !at_start {
            let mut cut_first = f64::NAN;
            let mut k = knot_of(inp.s, a0);
            if inp.s[k] >= a0 && k > 0 { k -= 1; }
            loop {
                let sk = inp.s[k];
                if sk < a0 - X_MAX || sk < lo_lim { break; }
                let c = cover_at(inp, k);
                if cut_first.is_nan() { cut_first = c; }
                if c <= d_cut { break; }
                a = sk;
                if k == 0 { break; }
                k -= 1;
            }
            if a < a0 { snaps.push(Snap { mapped: a0, built: a, cut: cut_first }); }
        }
        if !join1 && !at_end {
            let mut cut_first = f64::NAN;
            let mut k = knot_of(inp.s, b0);
            if inp.s[k] <= b0 { k += 1; }
            while k < n {
                let sk = inp.s[k];
                if sk > b0 + X_MAX || sk > hi_lim { break; }
                let c = cover_at(inp, k);
                if cut_first.is_nan() { cut_first = c; }
                if c <= d_cut { break; }
                b = sk;
                k += 1;
            }
            if b > b0 { snaps.push(Snap { mapped: b0, built: b, cut: cut_first }); }
        }
        // the knots inside [a, b] (both ends as stations of their own)
        let mut st: Vec<f64> = vec![a];
        for &x in inp.s.iter() { if x > a + 1e-6 && x < b - 1e-6 { st.push(x); } }
        st.push(b);
        let interp = |arr: &dyn Fn(usize) -> f64, x: f64| -> f64 {
            let k = knot_of(inp.s, x).min(n - 2);
            let l = inp.s[k + 1] - inp.s[k];
            let u = if l > 0.0 { ((x - inp.s[k]) / l).max(0.0).min(1.0) } else { 0.0 };
            arr(k) * (1.0 - u) + arr(k + 1) * u
        };
        let cov: Vec<f64> = st.iter().map(|&x| interp(&|k| cover_at(inp, k), x)).collect();
        let zs: Vec<f64> = st.iter().map(|&x| interp(&|k| inp.z[k], x)).collect();
        let side_hi = |k: usize, l: bool| -> f64 {
            let v = if l { &inp.side_l[k] } else { &inp.side_r[k] };
            let mut m = f64::NEG_INFINITY;
            for &x in v.iter() { if x.is_finite() && x > m { m = x; } }
            m
        };
        // kinds per station: a daylight candidate, else full tube / lid
        let mut kind: Vec<u8> = Vec::with_capacity(st.len());
        let mut cand: Vec<bool> = Vec::with_capacity(st.len());
        for (j, &x) in st.iter().enumerate() {
            let k = knot_of(inp.s, x);
            let (hl, hr) = (side_hi(k, true), side_hi(k, false));
            cand.push(cov[j] < GAP_COVER && hl < zs[j] + GAP_COVER && hr < zs[j] + GAP_COVER && hl.is_finite() && hr.is_finite());
            kind.push(if cov[j] >= TUBE_H + LID_MARGIN { 2 } else { 1 });
        }
        // daylight gaps: candidate runs of GAP_MIN_M or more, not touching the range's ends (a portal is no gap)
        let mut j = 0;
        while j < st.len() {
            if !cand[j] { j += 1; continue; }
            let mut e = j;
            while e + 1 < st.len() && cand[e + 1] { e += 1; }
            if st[e] - st[j] >= GAP_MIN_M && j > 0 && e + 1 < st.len() { for q in j..=e { kind[q] = 0; } }
            j = e + 1;
        }
        // runs, short ones joined to the run before (the first to the next)
        let mut rr: Vec<(usize, usize, u8)> = Vec::new();
        let mut j = 0;
        while j < st.len() {
            let mut e = j;
            while e + 1 < st.len() && kind[e + 1] == kind[j] { e += 1; }
            rr.push((j, e, kind[j]));
            j = e + 1;
        }
        let mut merged: Vec<(usize, usize, u8)> = Vec::new();
        for r in rr {
            let short = st[r.1] - st[r.0] < MIN_RUN_M && r.2 != 0;
            if short {
                if let Some(l) = merged.last_mut() { l.1 = r.1; continue; }
            }
            if let Some(l) = merged.last_mut() {
                if l.2 == r.2 { l.1 = r.1; continue; }
                if l.2 != 0 && st[l.1] - st[l.0] < MIN_RUN_M { l.1 = r.1; l.2 = r.2; continue; }
            }
            merged.push(r);
        }
        let m = merged.len();
        for (q, &(j0, j1, kd)) in merged.iter().enumerate() {
            let (s0, s1) = (st[j0], st[j1]);
            let mouth0 = if q == 0 { !join0 } else { merged[q - 1].2 == 0 && kd != 0 };
            let mouth1 = if q + 1 == m { !join1 } else { merged[q + 1].2 == 0 && kd != 0 };
            let (mouth0, mouth1) = if kd == 0 { (false, false) } else { (mouth0, mouth1) };
            // least inner cover (2 m in from each end where the run is long enough)
            let mut cmin = f64::INFINITY;
            for jj in j0..=j1 {
                if j1 - j0 >= 2 && (st[jj] < s0 + 2.0 || st[jj] > s1 - 2.0) { continue; }
                if cov[jj] < cmin { cmin = cov[jj]; }
            }
            if !cmin.is_finite() { cmin = cov[j0].min(cov[j1]); }
            // the holes: from each portal inward to where the vault top + HOLE_OVER is under the footprint's ground
            let reach = |from_start: bool| -> f64 {
                let mut run = 0;
                let idx: Vec<usize> = if from_start { (j0..=j1).collect() } else { (j0..=j1).rev().collect() };
                for &jj in &idx {
                    let d = if from_start { st[jj] - s0 } else { s1 - st[jj] };
                    if d > HOLE_MAX_M { return HOLE_MAX_M; }
                    let k = knot_of(inp.s, st[jj]);
                    let fg = foot_ground(inp, k);
                    if zs[jj] + TUBE_H + HOLE_OVER <= fg { run += 1; if run >= 2 { return d; } } else { run = 0; }
                }
                s1 - s0
            };
            let hole0 = if mouth0 { reach(true) } else { 0.0 };
            let hole1 = if mouth1 { reach(false) } else { 0.0 };
            runs.push(Run { s0, s1, kind: kd, mouth0, mouth1, cmin, hole0, hole1 });
        }
    }
    (runs, snaps)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(n: usize, h: f64) -> Vec<f64> { (0..n).map(|i| i as f64 * h).collect() }

    #[test]
    fn a_hill_tunnel_is_one_tube_with_portals_and_holes_only_near_them() {
        // 1 km at 5 m knots, floor flat at 100 m, a hill from 300 to 700 m rising to 160 m (mapped tunnel 320..680)
        let s = line(201, 5.0);
        let z = vec![100.0; 201];
        let g: Vec<f64> = s.iter().map(|&x| if x < 300.0 || x > 700.0 { 100.0 } else { 100.0 + 60.0 * (1.0 - ((x - 500.0) / 200.0).abs()) }).collect();
        let side: Vec<[f64; 3]> = g.iter().map(|&v| [v, v, v]).collect();
        let inp = StrokeIn { s: &s, z: &z, g: &g, side_l: &side, side_r: &side, ranges: &[(320.0, 680.0)], joined0: false, joined1: false, kmh: 80.0 };
        let (runs, snaps) = stroke_record(&inp);
        assert!(snaps.is_empty(), "{:?}", snaps);
        // thin cover at the ends (lid) then a full tube
        assert!(runs.iter().all(|r| r.kind != 0), "{:?}", runs);
        assert!(runs.first().unwrap().mouth0 && runs.last().unwrap().mouth1);
        assert_eq!(runs.first().unwrap().s0, 320.0);
        assert_eq!(runs.last().unwrap().s1, 680.0);
        let full: Vec<&Run> = runs.iter().filter(|r| r.kind == 2).collect();
        assert_eq!(full.len(), 1, "{:?}", runs);
        // the holes end where the vault + 1 m is under the ground: ground 100 + 0.3 x (x - 300) >= 107.8 from ~326 m
        let h0 = runs.first().unwrap().hole0;
        assert!(h0 > 0.0 && h0 < 20.0, "{:?}", runs);
    }

    #[test]
    fn a_tunnel_under_thin_ground_is_a_lid_not_an_open_road_and_a_real_daylight_gap_stays_open() {
        let s = line(101, 5.0);
        let z = vec![100.0; 101];
        let mut g = vec![101.0; 101];         // 1 m of cover all along (I-395 under its lid)
        let mut side: Vec<[f64; 3]> = g.iter().map(|&v| [v, v, v]).collect();
        let inp = StrokeIn { s: &s, z: &z, g: &g, side_l: &side, side_r: &side, ranges: &[(50.0, 450.0)], joined0: false, joined1: false, kmh: 50.0 };
        let (runs, _) = stroke_record(&inp);
        // the side ground is 1 m over the floor too: a candidate gap all along, but one touching both ends is no gap
        assert_eq!(runs.len(), 1, "{:?}", runs);
        assert_eq!(runs[0].kind, 1);
        // now a valley in the middle (200..300 m) with the ground on both sides low: a daylight gap
        for i in 0..101 { if s[i] >= 200.0 && s[i] <= 300.0 { g[i] = 100.5; side[i] = [100.5, 100.5, 100.5]; } else { g[i] = 110.0; side[i] = [110.0, 110.0, 110.0]; } }
        let inp = StrokeIn { s: &s, z: &z, g: &g, side_l: &side, side_r: &side, ranges: &[(50.0, 450.0)], joined0: false, joined1: false, kmh: 50.0 };
        let (runs, _) = stroke_record(&inp);
        let kinds: Vec<u8> = runs.iter().map(|r| r.kind).collect();
        assert_eq!(kinds, vec![2, 0, 2], "{:?}", runs);
        assert!(runs[0].mouth1 && runs[2].mouth0, "{:?}", runs);
    }

    #[test]
    fn rule_p_moves_a_portal_out_at_most_x_max_and_never_in_and_not_at_a_joint() {
        let s = line(101, 5.0);
        let z = vec![100.0; 101];
        // the ground stays 20 m over the floor for 20 m past the mapped start (200 m), then drops
        let g: Vec<f64> = s.iter().map(|&x| if x >= 180.0 { 120.0 } else { 101.0 }).collect();
        let side: Vec<[f64; 3]> = g.iter().map(|&v| [v, v, v]).collect();
        let inp = StrokeIn { s: &s, z: &z, g: &g, side_l: &side, side_r: &side, ranges: &[(200.0, 400.0)], joined0: false, joined1: false, kmh: 80.0 };
        let (runs, snaps) = stroke_record(&inp);
        assert_eq!(snaps.len(), 2, "{:?}", snaps);       // (the far end too: the hill goes on past 400 m)
        assert_eq!(snaps[0].built, 180.0);
        assert_eq!(runs[0].s0, 180.0);
        assert_eq!(runs.last().unwrap().s1, 430.0);       // X_MAX past the mapped end
        // a town road: the same 20 m cut is over D_CUT_TOWN too; joined ends never move
        let inp = StrokeIn { s: &s, z: &z, g: &g, side_l: &side, side_r: &side, ranges: &[(0.0, 400.0)], joined0: true, joined1: false, kmh: 30.0 };
        let (runs, _) = stroke_record(&inp);
        assert_eq!(runs[0].s0, 0.0);
        assert!(!runs[0].mouth0);
    }
}
