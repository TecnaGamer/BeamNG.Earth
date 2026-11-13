//! grading.grade_points: the ground at points graded under the road plans' segments (the corridor apply: a terrain
//! block's 1024² / 2048² samples, road tiles, far tiles). The per-pair arithmetic is shared by two traversals that pair
//! each point with the same segments in the same order, so their heights are bit for bit the same:
//!
//! - `bucketed` (lib.rs grade_points, the reference): segments outer, the points sorted into BUCKET m squares, each
//!   segment's squares within its reach walked row by row, the points' state in arrays (~180 B per point).
//! - `binned` (grade_points_binned, the default): the segments put once into fine bins over the points (only bins
//!   within each segment's reach of an effect), then each bin's points against its own short list, segment by
//!   segment (the bin's points' distances side by side), two passes per point with its state in a small buffer,
//!   without the GIL. A point is paired with a segment only where the bucketed walk pairs them (its square inside the
//!   segment's squares: the reference's visits, replayed as a test), so the pairs left out are the ones past every
//!   effect (the squares' corners). A 2048² terrain block's grading 2.2-2.8x less CPU (2026-11-11: Roswell NM
//!   2.6 -> 1.1 s, Fells Point 2.4 -> 1.0 s, the Stelvio 0.7 -> 0.35 s), a road tile's ~2x.
//!
//! The reference's squares make a pair's presence depend on the batch (a cutting whose top reaches past its box's
//! reach is seen only by the points whose 32 m square its box touches); binned keeps that, for the same heights.
//! Dropping `Visit::takes` would make the heights batch-independent (with a format bump: they change there).
//!
//! Change the arithmetic in `pair0` / `pair1` / `finish` only: both traversals call them.

use numpy::ndarray::{ArrayView1, ArrayView2};

/// a road's own rid of its rid (grading.PlanPack: a pass road's legs carry their leg in the bits from 44)
pub const LEG_ROAD_MASK: i64 = (1i64 << 44) - 1;
/// a built road's grade rank (grading.GRADE_RANK_BUILT): only these reach past their core over rank_fade (the side
/// road's ease; an unpaved road's rank is below it)
pub const RANK_BUILT: i64 = 100;
/// grade ranks within this of the higher road's take the same side ease (a service road or driveway beside a
/// residential street); a bigger class gap (a residential approach to a primary) keeps the old blend, whose side
/// ease cut that approach to a 67 % step
pub const RANK_SIDE_MAX_DIFF: i64 = 6;
/// the hard hold of a road's own carriageway applies to primary and below; a motorway or trunk keeps the old blend
/// (its split parts' seams are their own road, not a subordinate one, and the seam regression test needs the old
/// smoothness there)
pub const RANK_HOLD_MAX: i64 = 114;
/// a segment's flags (grading.PlanPack's st): a mountain pass road's (Plan.steep: its whole core its carriageway, its
/// sides falling at steep_run) ...
pub const STEEP: u8 = 1;
/// ... and a tunnel's open section (Plan.open_tunnel: the map's tunnel on the ground, where the elevation shows no
/// cover over it). A lower-ranked road crossing it (60 degrees or more, fading out by 30) at a point it is the
/// highest-ranked road of takes the side ease onto its ground, whatever the class gap (RANK_SIDE_MAX_DIFF is for a
/// road's own junction approaches): such a road is mapped over the tunnel, not on its ground, and held to the old
/// blend it stepped down to the section's held carriageway at its edge (Kennedybruecke in Vienna: a bus lane over the
/// open "Unterfahrung" stepped 1.2 m, GitHub #1169).
pub const OPEN_TUNNEL: u8 = 2;
/// the steepest a segment's line is carried on past its end (extrap): steeper than any road's grade; a steeper
/// segment (a step held in a plan over a few cm) is carried on at this
pub const EXTRAP_MAX_GRADE: f64 = 0.35;

/// The scalar knobs (grading's constants, in grade_points' argument order).
#[derive(Clone, Copy)]
pub struct Prm {
    pub extrap: f64, pub lift: f64, pub slope_run: f64, pub t_min: f64, pub t_max: f64, pub steep_run: f64,
    pub steep_t_min: f64, pub blend_r: f64, pub bucket: f64, pub wall_cut: f64, pub wall_t: f64, pub wall_flat: f64,
    pub wall_ahead: f64, pub sep: bool, pub sep_from: f64, pub sep_full: f64, pub core_fade: f64, pub side_fade: f64,
    pub sharp_step: f64, pub yield_ramp: f64, pub rank_fade: f64, pub rank_height_fade: f64, pub abut_under: f64,
}

/// The segments' arrays (grading.PlanPack's, in its order).
pub struct Segs<'a> {
    pub a: ArrayView2<'a, f64>, pub b: ArrayView2<'a, f64>, pub za: ArrayView1<'a, f64>, pub zb: ArrayView1<'a, f64>,
    pub h: ArrayView1<'a, f64>, pub rh: ArrayView1<'a, f64>, pub rid: ArrayView1<'a, i64>, pub rk: ArrayView1<'a, i64>,
    pub abut: ArrayView1<'a, u8>, pub cut: ArrayView2<'a, f64>, pub ba: ArrayView1<'a, f64>,
    pub bb: ArrayView1<'a, f64>, pub steep: ArrayView1<'a, u8>, pub link: ArrayView1<'a, u8>,
    pub abut_d: ArrayView2<'a, f64>,
}

/// One segment as the pairs read it (its arrays' values, and what the reference derived from them per pair: the
/// same expressions, so the same values).
#[derive(Clone, Copy)]
struct Seg {
    ax: f64, ay: f64, abx: f64, aby: f64, l2: f64,
    lox: f64, hix: f64, loy: f64, hiy: f64,     // its box (min / max of its ends)
    len: f64,       // l2.sqrt()
    ext: f64,       // extrap / len
    za: f64, zb: f64, h: f64, rh: f64, rid: i64, rk: i64,
    abut: bool, steep: bool, open: bool, link: u8, banked: bool, has_cut: bool, cut_reach: f64, ba: f64, bb: f64,
    cut: [f64; 6], da: f64, db: f64, far0: f64, far0_base: f64,
}

fn prep(s: &Segs, i: usize, p: &Prm) -> Seg {
    let cut = [s.cut[[i, 0]], s.cut[[i, 1]], s.cut[[i, 2]], s.cut[[i, 3]], s.cut[[i, 4]], s.cut[[i, 5]]];
    let has_cut = cut.iter().any(|v| v.is_finite());
    let r = cut[2].max(cut[5]).max(cut[0]).max(cut[3]);
    let cut_reach = if r.is_finite() { r + 0.5 + 2.0 * p.sharp_step } else { 0.0 };
    let (ax, ay, bx, by) = (s.a[[i, 0]], s.a[[i, 1]], s.b[[i, 0]], s.b[[i, 1]]);
    let (abx, aby) = (bx - ax, by - ay);
    let (h, rh, rk) = (s.h[i], s.rh[i], s.rk[i]);
    // pass 0's farthest effect: a carriageway (rh), an abutment (h), a cutting's band (h + wall_flat) and top
    // (cut_reach: its side and ahead limits), a core (h + core_fade), a higher road's rank influence
    // (h + rank_fade: the side road's ease onto it); a margin for the rounding
    let far0_base = rh.max(h + if has_cut { p.wall_flat.max(0.0) } else { 0.0 }).max(cut_reach)
        .max(h + if p.sep { p.core_fade } else { 0.0 }) + 1e-6;
    let far0 = far0_base.max(h + if rk >= RANK_BUILT { p.rank_fade } else { 0.0 } + 1e-6);
    let l2 = abx * abx + aby * aby;
    Seg {
        ax, ay, abx, aby, l2, lox: ax.min(bx), hix: ax.max(bx), loy: ay.min(by), hiy: ay.max(by), len: l2.sqrt(), ext: p.extrap / l2.sqrt(), za: s.za[i], zb: s.zb[i], h, rh, rid: s.rid[i], rk,
        abut: s.abut[i] != 0, steep: s.steep[i] & STEEP != 0, open: s.steep[i] & OPEN_TUNNEL != 0,
        link: s.link[i], banked: s.ba[i] != 0.0 || s.bb[i] != 0.0,
        has_cut, cut_reach, ba: s.ba[i], bb: s.bb[i], cut, da: s.abut_d[[i, 0]], db: s.abut_d[[i, 1]], far0, far0_base,
    }
}

/// A segment's reach in a pass (the reference's box half widths): pass 0 within the blend's reach too (the Python
/// loop paired the points with a segment once, at h + t_max), and a built road's rank to rank_fade past its core
/// (every point that far, not those a bucket of the batch's grid happened to take in, or the result depended on the
/// batch, test_smooth_road_collision)
fn reach(g: &Seg, pass: usize, p: &Prm) -> f64 {
    if pass == 0 {
        (g.h + if p.sep { p.core_fade } else { 0.0 }).max(g.cut_reach).min(g.h + p.t_max)
            .max(if g.rk >= RANK_BUILT { g.h + p.rank_fade } else { 0.0 })
    } else {
        g.h + p.t_max
    }
}

/// One point's state over its pairs.
#[derive(Clone, Copy)]
pub struct Pt {
    num: f64, den: f64, wmax: f64,
    // two passes: the highest core of a road's last stretch before a bridge (abut) over the point, then the blend
    // without the roads more than wall_cut under it beside them, outside their own cores (#38)
    top: f64,
    // (sep) how much the point is in a core, and that core's height: other roads beside it count less (#92)
    cmax: f64, csum: f64, cz: f64,
    raised: f64,        // (sharp) the cuttings' tops over the point beside them
    bs: f64, beff: f64,
    // the carriageway the point is deepest in (d - rh, most negative) and its height there
    cdeep: f64, carz: f64, carid: i64,
    // the cutting band the point is deepest in (d - its far edge) and its road and height
    bdeep: f64, bz: f64, bid: i64,
    // (rank) the highest-ranked road whose core the point lies in (d - its core half width, negative inside), its
    // road, its surface height there, and how much of its ground it holds (1 across the carriageway, easing to 0
    // over its shoulder: a lower road's ground blends in from the painted edge out): see pass 1. hrank2: the
    // second-highest rank there (equal to hrank when another road of its rank is at the point, so its carriageway
    // is not held over that one's)
    hdeep: f64, hrank: i64, hrank2: i64, hid: i64, hz: f64, hyield: f64, hcar: bool, hdx: f64, hdy: f64,
    // (rank) that road is a tunnel's open section (OPEN_TUNNEL)
    hopen: bool,
}

impl Pt {
    #[inline(always)]
    pub fn new(base: f64) -> Pt {
        Pt {
            num: 0.0, den: 0.0, wmax: 0.0, top: f64::NEG_INFINITY, cmax: 0.0, csum: 0.0, cz: 0.0,
            raised: f64::NEG_INFINITY, bs: base, beff: base, cdeep: 0.0, carz: f64::NAN, carid: -1, bdeep: 0.0,
            bz: f64::NAN, bid: -1, hdeep: f64::INFINITY, hrank: i64::MIN, hrank2: i64::MIN, hid: -1, hz: f64::NAN,
            hyield: 0.0, hcar: false, hdx: 0.0, hdy: 0.0, hopen: false,
        }
    }

    /// Between the passes: a cutting's top raises the ground the blend starts from (sharp: any cut among the segments)
    #[inline(always)]
    fn between(&mut self, sharp: bool) {
        if sharp {
            self.beff = self.bs.max(self.raised);
        }
    }

    /// The point's height after both passes.
    #[inline(always)]
    fn finish(&self, lift: f64) -> f64 {
        // (a road whose rank is the point's highest, with no equal-ranked road there, holds its own carriageway whole:
        // a subordinate road's carriageway, cutting band or ground may not move it, not even by centimetres)
        if self.hcar && self.hrank > self.hrank2 && self.hrank <= RANK_HOLD_MAX {
            self.hz + lift
        } else if self.den > 0.0 {
            self.beff + self.wmax * (self.num / self.den - self.beff + lift)
        } else {
            self.beff
        }
    }
}

const OVERLAP_YIELD_M: f64 = 1.0;   // m (pass 1's overlap yield)

#[inline(always)]
fn smooth(v: f64) -> f64 {
    let v = v.clamp(0.0, 1.0);
    v * v * (3.0 - 2.0 * v)
}

/// The point (x, y) against segment g: (qx, qy) from its start, tu its parameter along the line (0 on a segment
/// shorter than 1e-6 m), t that within the segment, d its distance (not hypot: libm's is ~5x dearer, the pairs
/// loop's main cost).
#[inline(always)]
fn dist(g: &Seg, x: f64, y: f64) -> (f64, f64, f64, f64, f64) {
    let (qx, qy) = (x - g.ax, y - g.ay);
    let tu = if g.l2 > 1e-12 { (qx * g.abx + qy * g.aby) / g.l2 } else { 0.0 };
    let t = if g.l2 > 1e-12 { tu.clamp(0.0, 1.0) } else { 0.0 };
    let (ddx, ddy) = (qx - t * g.abx, qy - t * g.aby);
    (qx, qy, tu, t, (ddx * ddx + ddy * ddy).sqrt())
}

/// The points' (qx, qy, tu, t, d) against one segment.
#[derive(Default)]
struct Lanes {
    qx: Vec<f64>, qy: Vec<f64>, tu: Vec<f64>, t: Vec<f64>, d: Vec<f64>,
}

impl Lanes {
    fn resize(&mut self, m: usize) {
        for v in [&mut self.qx, &mut self.qy, &mut self.tu, &mut self.t, &mut self.d] {
            v.resize(m, 0.0);
        }
    }

    /// dist for every point (xs, ys): the same values, lane by lane (the divisions and roots side by side).
    #[inline(always)]
    fn dists(&mut self, g: &Seg, xs: &[f64], ys: &[f64]) {
        let m = xs.len();
        let (xs, ys) = (&xs[..m], &ys[..m]);
        let (qxs, qys, tus, ts, ds) = (&mut self.qx[..m], &mut self.qy[..m], &mut self.tu[..m], &mut self.t[..m],
                                       &mut self.d[..m]);
        if g.l2 > 1e-12 {
            for j in 0..m {
                let (qx, qy) = (xs[j] - g.ax, ys[j] - g.ay);
                let tu = (qx * g.abx + qy * g.aby) / g.l2;
                let t = tu.clamp(0.0, 1.0);
                let (ddx, ddy) = (qx - t * g.abx, qy - t * g.aby);
                qxs[j] = qx;
                qys[j] = qy;
                tus[j] = tu;
                ts[j] = t;
                ds[j] = (ddx * ddx + ddy * ddy).sqrt();
            }
        } else {
            for j in 0..m {
                let (qx, qy) = (xs[j] - g.ax, ys[j] - g.ay);
                let t = 0.0;
                let (ddx, ddy) = (qx - t * g.abx, qy - t * g.aby);
                qxs[j] = qx;
                qys[j] = qy;
                tus[j] = 0.0;
                ts[j] = t;
                ds[j] = (ddx * ddx + ddy * ddy).sqrt();
            }
        }
    }

    /// pre for the points sel (into pr), one after another before their pairs' pass.
    #[inline(always)]
    fn pres<const SIDE: bool>(&self, g: &Seg, p: &Prm, sel: &[usize], pr: &mut Vec<Pre>) {
        pr.clear();
        for &j in sel {
            pr.push(pre::<SIDE>(g, self.qx[j], self.qy[j], self.tu[j], self.t[j], self.d[j], p));
        }
    }
}

/// What a pair's passes read of the point and the segment: qx, qy from its start, t along it, d from it; the segment's
/// target height there, how far past an abutment end it lies, the cutting's (wall, top, top offset) on the point's
/// side; side: off its line, ahead: past its ends.
#[derive(Clone, Copy, Default)]
struct Pre {
    qx: f64, qy: f64, d: f64, target: f64, past: f64, cwall: f64, ctop: f64, creach: f64, side: f64, ahead: f64,
}

/// The Pre of the point (qx, qy from the segment's start, tu / t along it, d from it); SIDE: its side and ahead too
/// (pass 1's).
#[inline(always)]
fn pre<const SIDE: bool>(g: &Seg, qx: f64, qy: f64, tu: f64, t: f64, d: f64, p: &Prm) -> Pre {
    // (along the segment from its start: an abutment's and pass 1's)
    let along = if g.l2 > 1e-12 && (SIDE || g.abut) { (qx * g.abx + qy * g.aby) / g.len } else { 0.0 };
    let (target, past, cwall, ctop, creach) = target(g, qx, qy, tu, t, along, p);
    // a cut deeper than wall_cut: a retaining wall at the core's edge, the ground right behind it (for a point more
    // beside the segment than ahead of it: ahead, the road's own next segments blend in along it, and cutting those
    // off made a kink on every crest): pass 1
    let (mut side, mut ahead) = (0.0, 0.0);
    if SIDE {
        side = if g.l2 > 1e-12 { (qx * g.aby - qy * g.abx).abs() / g.len } else { d };
        ahead = if g.l2 > 1e-12 { (-along).max(along - g.len).max(0.0) } else { 0.0 };
    }
    Pre { qx, qy, d, target, past, cwall, ctop, creach, side, ahead }
}

/// The segment's target height for the point (qx, qy from its start, tu / t along it; along: m from its start along
/// it, where it is longer than 1e-6 m and leads onto a bridge) and how far past an abutment end it lies; then the
/// cutting's (wall, top, top offset) on the point's side.
#[inline(always)]
#[allow(clippy::too_many_arguments)]
fn target(g: &Seg, qx: f64, qy: f64, tu: f64, t: f64, along: f64, p: &Prm) -> (f64, f64, f64, f64, f64) {
    let mut target = g.za + (g.zb - g.za) * t;
    if g.link != 0 && g.l2 > 1e-12 {
        let e = g.ext;
        let lo = if g.link & 1 != 0 { -e } else { 0.0 };
        let hi = if g.link & 2 != 0 { 1.0 + e } else { 1.0 };
        let tc = tu.clamp(lo, hi);
        target = g.za + (g.zb - g.za) * tc;
        // (carried on past an end at EXTRAP_MAX_GRADE at most: a tunnel section's last 0.14 m segment holding a 5.2 m
        // step from its floor to its graded portal, carried 4 m on, stood the ground 22 m over the road at Porte
        // Maillot and 14 m under it beside, GitHub #1083)
        let slope = (g.zb - g.za) / g.len;
        if slope.abs() > EXTRAP_MAX_GRADE && !(0.0..=1.0).contains(&tc) {
            let (base, over) = if tc > 1.0 { (g.zb, (tc - 1.0) * g.len) } else { (g.za, tc * g.len) };
            target = base + slope.clamp(-EXTRAP_MAX_GRADE, EXTRAP_MAX_GRADE) * over;
        }
    }
    // m past a bridge abutment's end (abut_d: how far along the road it lies from this segment): over the deck
    let mut past = 0.0f64;
    if g.abut && g.l2 > 1e-12 {
        let len = g.len;
        if g.da.is_finite() {
            past = past.max(-along - g.da);
        }
        if g.db.is_finite() {
            past = past.max(along - len - g.db);
        }
    }
    // there falling away at abut_under, not held at the end's height (the core's round end reached half its width
    // back over the deck; a deck climbing to its end stood under that ground: a 35 cm ledge at the I-405's overpass
    // ends, Discord 2026-11-05)
    if p.abut_under > 0.0 {
        target -= past * p.abut_under;
    }
    if g.banked && g.l2 > 1e-12 {    // (a banked curve: tilted across the carriageway, its edge height past)
        let off = ((qy * g.abx - qx * g.aby) / g.len).clamp(-g.rh, g.rh);
        target += (g.ba + (g.bb - g.ba) * t) * off;
    }
    let left = (qx * g.aby - qy * g.abx) < 0.0;
    let co = if left { 0 } else { 3 };
    (target, past, g.cut[co], g.cut[co + 1], g.cut[co + 2])
}

/// Pass 0 of one pair: the carriageway, rank, abutment, cutting and core state.
#[inline(always)]
fn pair0(st: &mut Pt, g: &Seg, q: &Pre, p: &Prm) {
    let (qx, qy, d) = (q.qx, q.qy, q.d);
    // (the reach test first: nothing below changes a pair past it)
    if d > g.far0 {
        return;
    }
    let (target, past, cwall, ctop, creach) = (q.target, q.past, q.cwall, q.ctop, q.creach);
    // (a pass road's whole core, its shoulder too: the leg above's fill reached onto the strip between its lanes and
    // its wall and stood there as a jagged fin, 2026-11-01)
    let cr = if g.steep { g.h } else { g.rh };
    // (and none past its abutment end, over the deck, from yield_ramp on: the round end of an approach's carriageway
    // reached over the railway under a short bridge, and the track's bed gave way to it there; GitHub #909)
    let car = past <= p.yield_ramp.max(1e-6);
    if car && (d - cr < st.cdeep || (d <= cr && st.carz.is_nan())) {
        st.cdeep = d - cr;
        st.carz = target;
        st.carid = g.rid;
    }
    // (rank) the highest-ranked road whose core holds the point (equal ranks: the deeper core); a plan leading onto a
    // bridge (abut) is taken as beside it, not over the point's ground, and a cutting's core ends at its wall (as
    // pass 1's hs / drop: the same ground reaches there)
    let hs0 = if cwall.is_finite() { g.h.min(cwall) } else { g.h };
    let drop0 = cwall.is_finite() && !ctop.is_finite() && d > cwall;
    if g.rk >= 0 && !g.abut && !drop0 && d - hs0 < p.rank_fade
        && (g.rk > st.hrank || (g.rk == st.hrank && d - hs0 < st.hdeep)) {
        st.hdeep = d - hs0;
        if g.rk > st.hrank || (st.hid != g.rid && g.rk == st.hrank) {
            st.hrank2 = st.hrank;
        }
        st.hrank = g.rk;
        st.hid = g.rid;
        st.hz = target;
        // (its ground is held across the carriageway; the blend starts at the painted edge, reaches the lower road's
        // full weight its shoulder plus rank_fade out: gentle on the side road too, not a step at the shoulder's edge)
        let shoulder = (hs0 - g.rh).max(0.0);
        let reach = shoulder + p.rank_fade;
        st.hyield = 1.0 - smooth(((d - g.rh) / reach).clamp(0.0, 1.0));
        st.hcar = d <= g.rh;
        st.hopen = g.open;
        st.hdx = g.abx / g.len.max(1e-9);       // (its direction there: the side ease below)
        st.hdy = g.aby / g.len.max(1e-9);
    }
    // (past the other effects' reach only the rank's own: the rest of pass 0 changes nothing there, and running it
    // for the rank's wider reach cost ~10 % of a city block's grading)
    if d > g.far0_base {
        return;
    }
    if g.abut && d <= g.h && target > st.top {
        st.top = target;
    }
    if g.has_cut && ctop.is_finite() {     // the ground raised to a cutting's top past its wall
        let lab0 = g.l2.max(1e-24).sqrt();
        let al0 = (qx * g.abx + qy * g.aby) / lab0;
        let ahead0 = (-al0).max(al0 - lab0).max(0.0);
        let side0 = (qx * g.aby - qy * g.abx).abs() / lab0;
        let bend = g.h.min(cwall) + p.wall_flat;     // the band behind its wall
        if side0 > g.rh && ahead0 < side0 && d - bend < st.bdeep {
            st.bdeep = d - bend;
            st.bz = target;
            st.bid = g.rid;
        }
        if side0 > cwall && side0 <= creach + 0.5 && ahead0 < p.sharp_step * 2.0 && ctop > st.raised {
            st.raised = ctop;
        }
    }
    if p.sep {
        let cn = 1.0 - smooth((d - g.h) / p.core_fade);
        if cn > 0.0 {
            if cn > st.cmax {
                st.cmax = cn;
            }
            st.csum += cn;
            st.cz += cn * target;
        }
    }
}

/// Pass 1 of one pair: its weight in the blend. far1: past it (d - h) no weight.
#[inline(always)]
fn pair1(st: &mut Pt, g: &Seg, q: &Pre, p: &Prm, far1: f64) {
    let d = q.d;
    // (past every blend's reach: no weight, nothing changes; the box's corners, ~half the pairs)
    if d - g.h > far1 {
        return;
    }
    let (target, cwall, ctop, side, ahead) = (q.target, q.cwall, q.ctop, q.side, q.ahead);
    // (and not far past a road's end: beside a tunnel the approach's last segment flattened a strip 10 m along it, a
    // pit beside the arch, 2026-10-17)
    // (on a cutting's side the core ends at its wall; where the ground falls away there the road ends)
    let hs = if cwall.is_finite() { g.h.min(cwall) } else { g.h };
    let drop = cwall.is_finite() && !ctop.is_finite() && d > cwall;
    let wall = st.beff - target > p.wall_cut && ahead < side && ahead < p.wall_ahead;
    let tt = if wall { p.wall_t }
             else if g.steep { (p.steep_run * (target - st.beff).abs()).clamp(p.steep_t_min, p.t_max) }
             else { (p.slope_run * (target - st.beff).abs()).clamp(p.t_min, p.t_max) };
    // a wall: flat on one terrain step behind it (no slope in front of its face), then up
    let u = ((d - hs - if wall { p.wall_flat } else { 0.0 }) / tt).clamp(0.0, 1.0);
    let w = 1.0 - u * u * (3.0 - 2.0 * u);
    // (another road's carriageway here, at another level: this road gives way past its own carriageway)
    // (a road's own carriageway never gives way: a hairpin's band over the leg above sank its lanes under the terrain,
    // the Stelvio Pass, the user's report 2026-10-21)
    // (a carriageway first: a road's cutting band reaching over another road's carriageway at another level (its
    // "climb" was that road's embankment) does not pull that road's ground; a bridge's abutment sank 2.5 m under the
    // deck into the street below, a wall cars hit, GitHub #108 Meppen)
    // (by how much: fully from wall_cut apart and yield_ramp past its carriageway, none from yield_ramp less and at its
    // edge; giving way at once there stepped a hairpin's lane 27 cm where its other leg crossed wall_cut, the Stelvio,
    // 2026-11-01)
    let ramp = p.yield_ramp.max(1e-6);
    let apart = |dz: f64| smooth((dz.abs() - p.wall_cut + ramp) / ramp);
    let mut yf = 0.0f64;
    if st.carid >= 0 && st.carid != g.rid {
        yf = apart(target - st.carz);
    }
    // (a cutting's band: another road's alone; a pass road's legs (grading.LEG_RID_SHIFT: the same road) blend over
    // each other's bands, the ground between them a slope: giving way there drew a 5 m step along the band's edge,
    // which notches at every vertex: spikes between a hairpin's legs, the user's report 2026-11-01. On each other's
    // lanes they still give way, above)
    if st.bid >= 0 && st.bid & LEG_ROAD_MASK != g.rid & LEG_ROAD_MASK {
        yf = yf.max(apart(target - st.bz));
    }
    // (two carriageways overlapping in plan at other levels, a wall between them: the point is the road's whose
    // carriageway it lies deeper in. This one gives way from where the point lies yield_ramp deeper in the other's
    // than in its own (st.cdeep: the deepest's d - rh, negative inside), not only past its own edge: Vienna's S1 east
    // carriageway in its trench rose 1.6 m where a 14 m bank ramp's carriageway reached 1.5 m over its lanes, GitHub
    // #1083, Rautenweg)
    // (only a higher road over a lower one's lanes: a road in its cutting is not raised by a bank road's, the bank
    // road's edge may still come down to it; at most OVERLAP_YIELD_M: only a carriageway's edge strip gives way so, two
    // crossing deep into each other (one that should be a bridge) still blend, a wall across the lanes there was worse,
    // Porte Maillot's Voie AX; none past a road's end, over a deck, whose own rules hold there, GitHub #909)
    let over = if st.carid >= 0 && st.carid != g.rid && target > st.carz && q.past <= 1e-6 {
        (-st.cdeep).clamp(0.0, OVERLAP_YIELD_M)
    } else {
        0.0
    };
    yf *= smooth((d - g.rh + over) / ramp);
    // (a lower-ranked road gives way over a higher-ranked one's ground (grading.grade_rank): a built (paved / cobble)
    // road keeps its own ground where an unpaved dirt road or track runs beside it, and a built side road eases onto
    // a higher built one the same way. On the higher road's carriageway the lower one gives way whole; past its
    // painted edge the lower road's own target is eased onto the higher road's ground over its shoulder plus
    // rank_fade, so the side road ramps gently instead of stepping at the shoulder's edge. Only roads at grade count
    // (an abutment or bridge is not on the point's ground), and a road's own other stretches never give way: hairpins
    // as before)
    let mut tgt = target;
    let ranked = g.rk < RANK_BUILT || st.hrank - g.rk <= RANK_SIDE_MAX_DIFF;
    let side_ease = ranked || st.hopen;
    if g.rk >= 0 && st.hrank > g.rk && side_ease && !g.abut && st.hid != g.rid {
        // (on the higher road's own carriageway its ground is held whole, whatever the height difference: a dirt path
        // 1-2 m lower beside the asphalt still left a dip in it. Past the painted edge the height fade decides: a road
        // far above or below is not on its ground)
        let rf = if st.hcar { 1.0 } else {
            let dz = (target - st.hz).abs();
            1.0 - smooth((dz - p.wall_cut).max(0.0) / p.rank_height_fade)
        };
        // (past the painted edge only a lower road running beside the higher one eases onto its ground: one leaving it,
        // a side road's mouth, keeps its own plan, which trim_side_mouths already landed on the edge. Eased onto the
        // shoulder too, a dirt road's mouth sat flat, then climbed at twice its grade to its plan: 0.9 g at 45 mph
        // where dirt meets asphalt, Discord 2026-11-06)
        // (an unpaved road only: paved side roads over Vienna's lids stepped 1-3 m without the ease, drivability)
        let align = if st.hcar || g.rk >= RANK_BUILT { 1.0 } else {
            let c = ((g.abx * st.hdx + g.aby * st.hdy) / g.len.max(1e-9)).abs();
            smooth((c - 0.5) / (0.866 - 0.5))
        };
        // (a tunnel's open section only: a road crossing it, mapped over the tunnel, eases onto it whatever the class
        // gap; one running beside it, a street along the top of its trench, keeps its own ground: a residential street
        // over the A23's cover beside its portal stepped 1.2 m at the cover's edge, eased down to the open section)
        let cross = if ranked { 1.0 } else {
            let c = ((g.abx * st.hdx + g.aby * st.hdy) / g.len.max(1e-9)).abs();
            1.0 - smooth((c - 0.5) / (0.866 - 0.5))
        };
        let raise = st.hyield * rf * align * cross;
        if st.hcar {
            yf = yf.max(raise);
        } else {
            tgt = target + (st.hz - target) * raise;
        }
    }
    if w > 0.0 && !drop && yf < 1.0 && !(d > hs && ahead < side && target < st.top - p.wall_cut) {
        let w = w * (1.0 - yf);
        let r = d / p.blend_r;
        let mut wb = w / (1.0 + r * r);
        if p.sep {
            let rf = st.cz / st.csum.max(1e-12);
            let f = st.cmax * smooth(((target - rf).abs() - p.sep_from) / (p.sep_full - p.sep_from))
                * smooth((d - hs) / p.core_fade) * smooth((side - ahead) / p.side_fade);
            wb *= 1.0 - f;
        }
        st.num += wb * tgt;
        st.den += wb;
        if w > st.wmax {
            st.wmax = w;
        }
    }
}

/// pass 1's reach past the core: a pair with d - h past this has u >= 1 (its core hs <= h, its ramp tt <= t_max, a
/// wall's flat and ramp wall_flat + wall_t), so w = 0: skipped (a margin for the rounding)
#[inline(always)]
fn far1(p: &Prm) -> f64 {
    p.t_max.max(p.wall_flat + p.wall_t) + 1e-6
}

/// The points' box as the reference takes it (f64::min / max: NaN coordinates passed over).
fn bounds(px: &ArrayView1<f64>, py: &ArrayView1<f64>) -> (f64, f64, f64, f64) {
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..px.len() {
        x0 = x0.min(px[i]); x1 = x1.max(px[i]);
        y0 = y0.min(py[i]); y1 = y1.max(py[i]);
    }
    (x0, y0, x1, y1)
}

/// The reference traversal (lib.rs grade_points): segments outer, then bucket rows, then the points sorted by bucket
/// (stable), the state per point in an array. `keep`: the segments taken part (None: all).
pub fn bucketed(px: ArrayView1<f64>, py: ArrayView1<f64>, base: ArrayView1<f64>, s: &Segs, p: &Prm,
                keep: Option<&[bool]>) -> Vec<f64> {
    let n = px.len();
    let mut out: Vec<f64> = base.iter().copied().collect();
    if n == 0 {
        return out;
    }
    // the segments taking part, in their order
    let segs: Vec<Seg> = (0..s.h.len()).filter(|&i| keep.map_or(true, |k| k[i])).map(|i| prep(s, i, p)).collect();
    let sharp = segs.iter().any(|g| g.has_cut);
    let (x0, y0, x1, y1) = bounds(&px, &py);
    let bucket = p.bucket;
    let bxs: Vec<i64> = (0..n).map(|i| ((px[i] - x0) / bucket).floor() as i64).collect();
    let bys: Vec<i64> = (0..n).map(|i| ((py[i] - y0) / bucket).floor() as i64).collect();
    let nbx = bxs.iter().copied().max().unwrap() + 1;
    let keys: Vec<i64> = (0..n).map(|i| bys[i] * nbx + bxs[i]).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| keys[i]);                    // stable, as argsort(kind="stable")
    let sk: Vec<i64> = order.iter().map(|&i| keys[i]).collect();
    // the points and their state in that order (the pairs loop reads them in runs)
    let xs: Vec<f64> = order.iter().map(|&i| px[i]).collect();
    let ys: Vec<f64> = order.iter().map(|&i| py[i]).collect();
    let mut st: Vec<Pt> = order.iter().map(|&i| Pt::new(base[i])).collect();
    let f1 = far1(p);
    for pass in 0..2 {
        if pass == 1 {
            for q in st.iter_mut() {
                q.between(sharp);
            }
        }
        for g in segs.iter() {
            let r = reach(g, pass, p);
            let (lo_x, hi_x, lo_y, hi_y) = seg_box(g, r);
            if hi_x < x0 || lo_x > x1 || hi_y < y0 || lo_y > y1 {
                continue;
            }
            let cx0 = 0i64.max(((lo_x - x0) / bucket).floor() as i64);
            let cx1 = (nbx - 1).min(((hi_x - x0) / bucket).floor() as i64);
            let cy0 = 0i64.max(((lo_y - y0) / bucket).floor() as i64);
            let cy1 = ((hi_y - y0) / bucket).floor() as i64;
            for row in cy0..=cy1 {
                let i0 = sk.partition_point(|&k| k < row * nbx + cx0);
                let i1 = sk.partition_point(|&k| k <= row * nbx + cx1);
                for idx in i0..i1.max(i0) {
                    let (qx, qy, tu, t, d) = dist(g, xs[idx], ys[idx]);
                    // (the reach tests before the pair's target, banking and cutting lookups)
                    if pass == 0 {
                        if d > g.far0 {
                            continue;
                        }
                        pair0(&mut st[idx], g, &pre::<false>(g, qx, qy, tu, t, d, p), p);
                    } else {
                        if d - g.h > f1 {
                            continue;
                        }
                        pair1(&mut st[idx], g, &pre::<true>(g, qx, qy, tu, t, d, p), p, f1);
                    }
                }
            }
        }
    }
    for j in 0..n {
        out[order[j]] = st[j].finish(p.lift);
    }
    out
}

/// A segment's box grown by r (from its ends, as the reference: min / max of a and b).
#[inline(always)]
fn seg_box(g: &Seg, r: f64) -> (f64, f64, f64, f64) {
    (g.lox - r, g.hix + r, g.loy - r, g.hiy + r)
}

/// The reference's visit of one segment in one pass: the bucket squares (cx0..=cx1, cy0..=cy1) whose points it pairs
/// with (NONE where it skips the segment: no square).
#[derive(Clone, Copy)]
struct Visit {
    cx0: i64, cx1: i64, cy0: i64, cy1: i64,
}

impl Visit {
    const NONE: Visit = Visit { cx0: 1, cx1: 0, cy0: 1, cy1: 0 };

    #[inline(always)]
    fn takes(&self, bx: i64, by: i64) -> bool {
        bx >= self.cx0 && bx <= self.cx1 && by >= self.cy0 && by <= self.cy1
    }

    fn some(&self) -> bool {
        self.cx0 <= self.cx1 && self.cy0 <= self.cy1
    }
}

/// m the fine bins' reach is grown by past a segment's and its squares' edges: far over the rounding (a point whose
/// computed distance is within reach lies within ~1e-12 m of it)
const BIN_MARGIN_M: f64 = 1e-3;

/// grading.grade_points by fine bins (see the module doc): the same heights as `bucketed`, bit for bit. bin_m: the
/// fine bins' size (m; grown where the points are sparse, at most ~2 bins per point).
pub fn binned(px: ArrayView1<f64>, py: ArrayView1<f64>, base: ArrayView1<f64>, s: &Segs, p: &Prm,
              keep: Option<&[bool]>, bin_m: f64) -> Vec<f64> {
    // (compiled for AVX2 too, picked where the CPU has it: the distances 4 lanes wide and floor() inline. The same
    // IEEE operations either way, so the same heights; no FMA: Rust never fuses a multiply and an add on its own)
    #[cfg(target_arch = "x86_64")]
    {
        if std::env::var_os("BNE_GRADE_NO_AVX2").is_none() && is_x86_feature_detected!("avx2") {
            return unsafe { binned_avx2(px, py, base, s, p, keep, bin_m) };
        }
    }
    binned_impl(px, py, base, s, p, keep, bin_m)
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn binned_avx2(px: ArrayView1<f64>, py: ArrayView1<f64>, base: ArrayView1<f64>, s: &Segs, p: &Prm,
                      keep: Option<&[bool]>, bin_m: f64) -> Vec<f64> {
    binned_impl(px, py, base, s, p, keep, bin_m)
}

#[inline(always)]
fn binned_impl(px: ArrayView1<f64>, py: ArrayView1<f64>, base: ArrayView1<f64>, s: &Segs, p: &Prm,
               keep: Option<&[bool]>, bin_m: f64) -> Vec<f64> {
    let n = px.len();
    let mut out: Vec<f64> = base.iter().copied().collect();
    if n == 0 {
        return out;
    }
    // the segments taking part, in their order (a road tile's few of a whole plan set's)
    let segs: Vec<Seg> = (0..s.h.len()).filter(|&i| keep.map_or(true, |k| k[i])).map(|i| prep(s, i, p)).collect();
    let ns = segs.len();
    let sharp = segs.iter().any(|g| g.has_cut);
    let (x0, y0, x1, y1) = bounds(&px, &py);
    let bucket = p.bucket;
    let f1 = far1(p);
    let pxv = px.as_slice().map_or_else(|| px.to_vec().into(), std::borrow::Cow::Borrowed);
    let pyv = py.as_slice().map_or_else(|| py.to_vec().into(), std::borrow::Cow::Borrowed);
    let (px, py) = (&pxv[..n], &pyv[..n]);
    // each point's bucket square, as the reference sorts it (a NaN coordinate: square 0, as its cast)
    let mut bxs: Vec<i64> = vec![0; n];
    let mut bys: Vec<i64> = vec![0; n];
    for i in 0..n {
        bxs[i] = ((px[i] - x0) / bucket).floor() as i64;
        bys[i] = ((py[i] - y0) / bucket).floor() as i64;
    }
    let nbx = bxs.iter().copied().max().unwrap() + 1;
    // each segment's visit per pass (the reference's squares; NONE: the segments it skips)
    let mut visits: [Vec<Visit>; 2] = [Vec::with_capacity(ns), Vec::with_capacity(ns)];
    for pass in 0..2 {
        for g in segs.iter() {
            let r = reach(g, pass, p);
            let (lo_x, hi_x, lo_y, hi_y) = seg_box(g, r);
            if hi_x < x0 || lo_x > x1 || hi_y < y0 || lo_y > y1 {
                visits[pass].push(Visit::NONE);
                continue;
            }
            let cx0 = 0i64.max(((lo_x - x0) / bucket).floor() as i64);
            let cx1 = (nbx - 1).min(((hi_x - x0) / bucket).floor() as i64);
            let cy0 = 0i64.max(((lo_y - y0) / bucket).floor() as i64);
            let cy1 = ((hi_y - y0) / bucket).floor() as i64;
            let v = Visit { cx0, cx1, cy0, cy1 };
            visits[pass].push(if v.some() { v } else { Visit::NONE });
        }
    }
    // the fine bins over the points' box (at most ~2 per point plus a few: a sparse batch's grown)
    let (wx, wy) = ((x1 - x0).max(0.0), (y1 - y0).max(0.0));
    if !(wx.is_finite() && wy.is_finite()) {      // (an infinite coordinate: the reference's walk)
        return bucketed(ArrayView1::from(px), ArrayView1::from(py), base, s, p, keep);
    }
    let mut fb = bin_m.max(0.5);
    let cap = (2 * n + 64) as f64;
    while ((wx / fb).floor() + 1.0) * ((wy / fb).floor() + 1.0) > cap {
        fb *= 1.5;
    }
    let fnx = (wx / fb).floor() as usize + 1;
    let fny = (wy / fb).floor() as usize + 1;
    let nb = fnx * fny;
    // the points by bin (counting sort; a NaN or out of range coordinate clamped: such a point pairs with nothing
    // anyway, see below)
    let mut pstart = vec![0u32; nb + 1];
    let mut pb: Vec<u32> = vec![0; n];
    for i in 0..n {
        let ix = ((px[i] - x0) / fb).floor();
        let iy = ((py[i] - y0) / fb).floor();
        let ix = if ix.is_finite() { (ix.max(0.0) as usize).min(fnx - 1) } else { 0 };
        let iy = if iy.is_finite() { (iy.max(0.0) as usize).min(fny - 1) } else { 0 };
        pb[i] = (iy * fnx + ix) as u32;
    }
    for &b in &pb {
        pstart[b as usize + 1] += 1;
    }
    for k in 0..nb {
        pstart[k + 1] += pstart[k];
    }
    let mut fill = pstart.clone();
    let mut porder = vec![0u32; n];
    for i in 0..n {
        let b = pb[i] as usize;
        porder[fill[b] as usize] = i as u32;
        fill[b] += 1;
    }
    drop(pb);
    // each pass's segments per bin, in segment order: every bin a segment's effect can reach (pass 0: far0, pass 1:
    // h + far1) within its visit's squares, its distance from the bin's centre within that plus the bin's half
    // diagonal (and a margin for the rounding)
    let half_diag = fb * std::f64::consts::FRAC_1_SQRT_2;
    let mut lists: Vec<(Vec<u32>, Vec<u32>)> = Vec::with_capacity(2);
    let mut found: Vec<(u32, u32)> = Vec::new();
    for pass in 0..2 {
        found.clear();
        for (si, g) in segs.iter().enumerate() {
            let v = visits[pass][si];
            if !v.some() {
                continue;
            }
            let r = if pass == 0 { g.far0 } else { g.h + f1 };
            let (lo_x, hi_x, lo_y, hi_y) = seg_box(g, r + BIN_MARGIN_M);
            // (and within the visit's squares)
            let lo_x = lo_x.max(x0 + v.cx0 as f64 * bucket - BIN_MARGIN_M);
            let hi_x = hi_x.min(x0 + (v.cx1 + 1) as f64 * bucket + BIN_MARGIN_M);
            let lo_y = lo_y.max(y0 + v.cy0 as f64 * bucket - BIN_MARGIN_M);
            let hi_y = hi_y.min(y0 + (v.cy1 + 1) as f64 * bucket + BIN_MARGIN_M);
            if !(lo_x <= hi_x && lo_y <= hi_y) {
                continue;
            }
            let bx0 = ((lo_x - x0) / fb).floor().max(0.0) as usize;
            let bx1 = (((hi_x - x0) / fb).floor().max(0.0) as usize).min(fnx - 1);
            let by0 = ((lo_y - y0) / fb).floor().max(0.0) as usize;
            let by1 = (((hi_y - y0) / fb).floor().max(0.0) as usize).min(fny - 1);
            if bx0 > bx1 || by0 > by1 {
                continue;
            }
            let lim = r + half_diag + BIN_MARGIN_M;
            for by in by0..=by1 {
                let cy = y0 + (by as f64 + 0.5) * fb;
                for bx in bx0..=bx1 {
                    let b = by * fnx + bx;
                    if pstart[b + 1] == pstart[b] {
                        continue;
                    }
                    let cx = x0 + (bx as f64 + 0.5) * fb;
                    if dist(g, cx, cy).4 <= lim {
                        found.push((b as u32, si as u32));
                    }
                }
            }
        }
        // (by bin, stable: each bin's segments in their order)
        let mut start = vec![0u32; nb + 1];
        for &(b, _) in &found {
            start[b as usize + 1] += 1;
        }
        for k in 0..nb {
            start[k + 1] += start[k];
        }
        let mut items = vec![0u32; found.len()];
        let mut at = start.clone();
        for &(b, si) in &found {
            items[at[b as usize] as usize] = si;
            at[b as usize] += 1;
        }
        lists.push((start, items));
    }
    drop(found);
    // each bin's points against its lists, segment by segment (each point still meets its segments in their order,
    // the bin's points' state in a small buffer: the pairs of one segment and the bin's points are independent, so
    // their divisions and roots overlap): pass 0, then pass 1
    let (s0, i0) = (&lists[0].0, &lists[0].1);
    let (s1, i1) = (&lists[1].0, &lists[1].1);
    let (mut xs, mut ys, mut bxv, mut byv, mut idv) = (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let mut sts: Vec<Pt> = Vec::new();
    let mut ln = Lanes::default();
    let mut sel: Vec<usize> = Vec::new();
    let mut pr: Vec<Pre> = Vec::new();
    for b in 0..nb {
        let (pa, pz) = (pstart[b] as usize, pstart[b + 1] as usize);
        if pa == pz {
            continue;
        }
        xs.clear(); ys.clear(); bxv.clear(); byv.clear(); idv.clear(); sts.clear();
        let (mut qx0, mut qx1, mut qy0, mut qy1) = (i64::MAX, i64::MIN, i64::MAX, i64::MIN);
        for &pi in &porder[pa..pz] {
            let i = pi as usize;
            let (x, y) = (px[i], py[i]);
            // (a NaN coordinate: no pair changes the reference's state, as no comparison holds; its bin's lists may
            // not hold every segment its square 0 meets, so it is left without pairs)
            if !(x.is_finite() && y.is_finite()) {
                let mut st = Pt::new(base[i]);
                st.between(sharp);
                out[i] = st.finish(p.lift);
                continue;
            }
            xs.push(x); ys.push(y); idv.push(i);
            bxv.push(bxs[i]); byv.push(bys[i]);
            qx0 = qx0.min(bxs[i]); qx1 = qx1.max(bxs[i]); qy0 = qy0.min(bys[i]); qy1 = qy1.max(bys[i]);
            sts.push(Pt::new(base[i]));
        }
        let m = xs.len();
        ln.resize(m);
        for pass in 0..2 {
            let l = if pass == 0 { &i0[s0[b] as usize..s0[b + 1] as usize] } else { &i1[s1[b] as usize..s1[b + 1] as usize] };
            for &si in l {
                let si = si as usize;
                let v = &visits[pass][si];
                let g = &segs[si];
                // (the bin's points' squares all inside the segment's: no test per point; none: no pairs)
                let all = qx0 >= v.cx0 && qx1 <= v.cx1 && qy0 >= v.cy0 && qy1 <= v.cy1;
                if !all && (qx1 < v.cx0 || qx0 > v.cx1 || qy1 < v.cy0 || qy0 > v.cy1) {
                    continue;
                }
                // every point's (qx, qy, t, d) at once (dist's arithmetic, IEEE in each lane: the same values, the
                // divisions and roots side by side instead of one pair's after another's), then the pairs within the
                // pass's reach (the pairs' own first test: those past it change nothing)
                ln.dists(g, &xs, &ys);
                let dds = &ln.d;
                sel.clear();
                if pass == 0 {
                    for j in 0..m {
                        if !(dds[j] > g.far0) && (all || v.takes(bxv[j], byv[j])) {
                            sel.push(j);
                        }
                    }
                    ln.pres::<false>(g, p, &sel, &mut pr);
                    for (k, &j) in sel.iter().enumerate() {
                        pair0(&mut sts[j], g, &pr[k], p);
                    }
                } else {
                    for j in 0..m {
                        if !(dds[j] - g.h > f1) && (all || v.takes(bxv[j], byv[j])) {
                            sel.push(j);
                        }
                    }
                    ln.pres::<true>(g, p, &sel, &mut pr);
                    for (k, &j) in sel.iter().enumerate() {
                        pair1(&mut sts[j], g, &pr[k], p, f1);
                    }
                }
            }
            if pass == 0 {
                for st in sts.iter_mut() {
                    st.between(sharp);
                }
            }
        }
        for j in 0..m {
            out[idv[j]] = sts[j].finish(p.lift);
        }
    }
    out
}
