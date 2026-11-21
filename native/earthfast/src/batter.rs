//! Vanilla-style cut and fill slopes beside the roads (road revamp v2 P1a, BNE_BATTER=vanilla; design
//! .claude/work/road-revamp-v2-road-shape-first-2026-11-19.md section 2.5, the stock maps' numbers in
//! vanilla-terrain-fusion-study-2026-11-19.md section 2.3).
//!
//! Today's grading (gradebins) blends a road's ground back to the natural over at most T_MAX (12 m) with a smoothstep:
//! 37-45 degrees and steeper beside any real cut or fill, a "carved ribbon". The stock maps hold a near-level strip, a
//! rounded crest of ~5 m, then a constant batter (1:2 rural, 1:3.5 suburban) for as far as the relief needs.
//!
//! Here each road segment gives, at a point x m past its core's edge (h), a corridor [Lo, Up] round its height z:
//!
//! ```text
//! strip   x < l0:            Lo = z - s_lo x,   Up = z + s_up x
//! crest   xb = x - l0 < c:   f = m xb^2 / (2 c)          (slope 0 -> m: rounded)
//! batter  xb >= c:           f = m (xb - c / 2)          (constant)
//! close   x > r - t:         f += k u^2 / (1 - u), u = (x - (r - t)) / t   (to infinity at its reach r)
//! ground = clamp(natural, max Lo, min Up)   (a smooth max / min of k_s at the daylight line)
//! ```
//!
//! z is the segment's height at the foot of the perpendicular, its line carried on a few metres past its ends (so a
//! graded road's neighbouring segments agree beside each other). max and min are exact and order free, every
//! segment's corridor is a continuous function of the point (its sides' slope and reach run linearly along it and
//! mix across its end caps), and a corridor closes at its reach: the ground is a continuous, pure function of
//! position and the plan set's segments, the same in every tile and request order, whatever other points a call holds
//! (each point meets every segment within its reach, nothing else). Where two corridors disagree (max Lo > min Up: two
//! roads at other heights close together) the ground runs between the two binding limits by their offsets (a ruled
//! surface from one road's edge to the other's).
//!
//! The slope m and reach r of each segment's side come from the natural ground sampled beside it once per plan set
//! (params: the class's batter, or a little steeper than the slope that daylights by some sample and stays out to the
//! farthest, the reach where it does; dilated along the plan so that neighbours differ by little) and from the
//! buildings beside it (a building within URBAN of the core: the urban batter; the corridor closes before the first
//! building on its side). A side the batter cannot reach the natural ground from at m_max before 40 m or a building,
//! or a sharp cutting's (its retaining wall), keeps today's grading where that holds for a run along its plan
//! ("old": its walls, its 12 m blend), as do the plans of no feature (tunnel mouths), a bridge's last metres (abut)
//! and a tunnel's open section: there and inside every core (and 1 m past it) today's heights stand, faded out over
//! old_fade past today's reach. Railway beds and roads dipped under decks (no grade rank, but their feature) get
//! batters too.

use numpy::ndarray::{ArrayView1, ArrayView2};

/// a side whose slope and reach are today's (flags)
pub const OLD: u8 = 1;

/// The params kernel's knobs.
#[derive(Clone, Copy)]
pub struct ParamPrm {
    pub l0: f64, pub crest: f64, pub s_lo: f64, pub s_up: f64, pub b_rural: f64, pub b_urban: f64, pub b_steep: f64,
    pub m_max: f64, pub r_max: f64, pub r_pad: f64, pub urban_m: f64, pub bld_gap: f64, pub dm: f64, pub dr: f64,
    pub gap_n: usize, pub run_n: usize, pub fit_k: f64, pub fit_add: f64, pub dil_n: usize,
}

/// A run of flags along a plan: gaps of up to gap_n false closed, then runs of fewer than run_n true cleared.
fn close_open(v: &mut [bool], gap_n: usize, run_n: usize) {
    let n = v.len();
    let mut i = 0;
    while i < n {           // (closing: a false gap between two true runs)
        if v[i] {
            i += 1;
            continue;
        }
        let mut e = i;
        while e < n && !v[e] {
            e += 1;
        }
        if i > 0 && e < n && e - i <= gap_n {
            for x in v.iter_mut().take(e).skip(i) {
                *x = true;
            }
        }
        i = e;
    }
    let mut i = 0;
    while i < n {           // (opening: short runs out)
        if !v[i] {
            i += 1;
            continue;
        }
        let mut e = i;
        while e < n && v[e] {
            e += 1;
        }
        if e - i < run_n {
            for x in v.iter_mut().take(e).skip(i) {
                *x = false;
            }
        }
        i = e;
    }
}

/// The apply kernel's knobs.
#[derive(Clone, Copy)]
pub struct ApplyPrm {
    pub l0: f64, pub crest: f64, pub s_lo: f64, pub s_up: f64, pub close_t: f64, pub close_k: f64, pub smooth_k: f64,
    pub core_blend: f64, pub old_reach: f64, pub old_fade: f64, pub lift: f64, pub bin_m: f64,
}

#[inline(always)]
fn smooth(v: f64) -> f64 {
    let v = v.clamp(0.0, 1.0);
    v * v * (3.0 - 2.0 * v)
}

/// The crest / batter offset's shape at xb m past the strip: f = m * shape (crest c).
#[inline(always)]
fn shape(xb: f64, c: f64) -> f64 {
    if xb <= 0.0 {
        0.0
    } else if xb < c {
        xb * xb / (2.0 * c)
    } else {
        xb - 0.5 * c
    }
}

/// The probe points beside each segment (a, b: (n, 2)): for each segment, its left side (the normal (-aby, abx)) then
/// its right, at its middle + normal * (h + offs[k]); and each side's ray start (the core's edge at its middle) and
/// direction. (pts (n * 2 * K, 2), origins (n * 2, 2), dirs (n * 2, 2))
pub fn probes(a: ArrayView2<f64>, b: ArrayView2<f64>, h: ArrayView1<f64>, offs: &[f64])
              -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let n = h.len();
    let k = offs.len();
    let mut pts = Vec::with_capacity(n * 2 * k * 2);
    let mut org = Vec::with_capacity(n * 4);
    let mut dir = Vec::with_capacity(n * 4);
    for i in 0..n {
        let (ax, ay, bx, by) = (a[[i, 0]], a[[i, 1]], b[[i, 0]], b[[i, 1]]);
        let (abx, aby) = (bx - ax, by - ay);
        let l = (abx * abx + aby * aby).sqrt();
        let (nx, ny) = if l > 1e-9 { (-aby / l, abx / l) } else { (0.0, 1.0) };
        let (mx, my) = (0.5 * (ax + bx), 0.5 * (ay + by));
        for side in [1.0f64, -1.0] {
            let (sx, sy) = (nx * side, ny * side);
            for &o in offs {
                pts.push(mx + sx * (h[i] + o));
                pts.push(my + sy * (h[i] + o));
            }
            org.push(mx + sx * h[i]);
            org.push(my + sy * h[i]);
            dir.push(sx);
            dir.push(sy);
        }
    }
    (pts, org, dir)
}

/// Each segment side's slope m and reach r (m past the core's edge) at its ends ((n, 4): left and right at a, then
/// at b) and its flags (n, 2), from the natural ground nat (n * 2 * K, as
/// probes lays them out) at offs past its core's edge and the distance to the first building wall from its core's
/// edge (bld (n * 2), infinite: none). Segment i: its height (za + zb) / 2, rk its grade rank and rid its feature
/// (rank < 0 and no feature, PlanPack's 2^40 + its index: a tunnel's mouth plan), abut
/// (near a bridge's end), st (gradebins' flags: STEEP, OPEN_TUNNEL), cut (n, 6) a sharp cutting's (left, then right:
/// wall offset, top height, top offset; a finite wall and top: a cutting with its wall), of: its plan (the slope and
/// reach dilated along it, dm and dr per segment: no flicker along a road).
#[allow(clippy::too_many_arguments)]
pub fn params(za: ArrayView1<f64>, zb: ArrayView1<f64>, rk: ArrayView1<i64>, rid: ArrayView1<i64>, abut: ArrayView1<u8>,
              st: ArrayView1<u8>, cut: ArrayView2<f64>, of: ArrayView1<i64>, nat: &[f64], bld: &[f64], offs: &[f64],
              p: &ParamPrm) -> (Vec<f64>, Vec<f64>, Vec<u8>) {
    let n = za.len();
    let k = offs.len();
    let mut m = vec![0.0f64; n * 2];
    let mut r = vec![0.0f64; n * 2];
    let mut fl = vec![0u8; n * 2];
    let mut soft = vec![false; n * 2];
    // (each side's hard limit: the reach cap, and a metre short of the first building on that side)
    let lim: Vec<f64> = (0..n * 2).map(|j| p.r_max.min(bld[j] - p.bld_gap)).collect();
    for i in 0..n {
        let z = 0.5 * (za[i] + zb[i]);
        let urban = bld[2 * i] <= p.urban_m || bld[2 * i + 1] <= p.urban_m;
        let steep = st[i] & crate::gradebins::STEEP != 0;
        let b = if steep { p.b_steep } else if urban { p.b_urban } else { p.b_rural };
        let m_class = 1.0 / b;
        // (no road and no feature: a tunnel's mouth plan, today's; a railway's bed and a road dipped under a deck have
        // no grade rank but their feature: their slopes are batters too, the I-94's railway cutting at NWAHV977 was
        // a trench of teeth)
        // (PlanPack's id of a plan without a feature: 2^40 + its index, planset.rs)
        let fid = rid[i] & crate::gradebins::LEG_ROAD_MASK;
        let no_feature = rid[i] < 0 || ((1i64 << 40)..(1i64 << 41)).contains(&fid);
        let seg_old = (rk[i] < 0 && no_feature) || abut[i] != 0 || st[i] & crate::gradebins::OPEN_TUNNEL != 0;
        for s in 0..2 {
            let j = 2 * i + s;
            // (a sharp cutting rising to a top: its retaining wall; a sharp fall (no top) is graded as a fill: today it
            // ends the road's ground at its wall, a drop to the natural ground drawn as teeth on the lattice)
            let side_cut = cut[[i, 3 * s]].is_finite() && cut[[i, 3 * s + 1]].is_finite();
            let nat_s = &nat[j * k..(j + 1) * k];
            let limit = p.r_max.min(bld[j] - p.bld_gap);
            // the required slope to daylight at each sample within the limit (NaN ground: a sample not read)
            let mut q = vec![f64::INFINITY; k];
            let mut jmax: isize = -1;
            let mut bad = false;
            for kk in 0..k {
                let o = offs[kk];
                // (a sample within the reach, and a metre short of a building: the batter must meet the ground
                // before it)
                if o > p.r_max || o + 1.0 > bld[j] - p.bld_gap {
                    break;
                }
                jmax = kk as isize;
                let dz = nat_s[kk] - z;
                if !dz.is_finite() {
                    bad = true;
                    break;
                }
                let allow = (if dz > 0.0 { p.s_up } else { p.s_lo }) * o.min(p.l0);
                let need = (dz.abs() - allow).max(0.0);
                let g = shape(o - p.l0, p.crest);
                q[kk] = if need <= 0.0 { 0.0 } else if g > 0.0 { need / g } else { f64::INFINITY };
            }
            if seg_old || bad || jmax < 0 {
                fl[j] = OLD;
                continue;
            }
            let jm = jmax as usize;
            // suffix maxima: the slope that daylights at sample kk and stays out to jm; the shallowest of them
            let mut suf = vec![0.0f64; jm + 1];
            let mut acc = 0.0f64;
            for kk in (0..=jm).rev() {
                acc = acc.max(q[kk]);
                suf[kk] = acc;
            }
            let fit = suf.iter().copied().fold(f64::INFINITY, f64::min);
            // (a little steeper than the fit where the class batter cannot reach: a clean crossing with the natural
            // ground, not a batter running along a slope as steep as itself, whose daylight line wanders far with each
            // undulation)
            let me = m_class.max(p.fit_k * fit + p.fit_add);
            if !(me <= p.m_max) {
                // (too steep for a batter before the limit: today's, if it holds for a run, else the steepest)
                soft[j] = true;
                m[j] = p.m_max;
                r[j] = limit;
            } else {
                let kd = (0..=jm).find(|&kk| suf[kk] <= me).unwrap_or(jm);
                m[j] = me;
                r[j] = (offs[kd] + p.r_pad).min(limit);
            }
            // (a sharp cutting's side: its retaining wall, today's, if it holds for a run)
            soft[j] |= side_cut;
        }
    }
    // the soft sides (a sharp cutting's, too steep for a batter) are today's only in runs along their plan: gaps of
    // up to gap_n segments closed, then runs shorter than run_n left out (flags flipping every few segments, the
    // elevation's own cutting edges along the I-94, drew the daylight line as teeth)
    for s in 0..2 {
        let mut i = 0;
        while i < n {
            let mut e = i + 1;
            while e < n && of[e] == of[i] {
                e += 1;
            }
            let mut v: Vec<bool> = (i..e).map(|q| soft[2 * q + s]).collect();
            close_open(&mut v, p.gap_n, p.run_n);
            for (k, q) in (i..e).enumerate() {
                if v[k] {
                    fl[2 * q + s] = OLD;
                }
            }
            i = e;
        }
    }
    // along each plan: a side's slope and reach the most of its neighbours' less dm / dr per segment between (a cone
    // dilation: never under its own, and neighbours a few metres apart differ by little; constant per segment, a step
    // of the slope between two drew the daylight line as teeth of the vertex step where the batter runs along the
    // natural ground). Today's sides take no part and pass the running values on.
    let mut i = 0;
    while i < n {
        let mut e = i + 1;
        while e < n && of[e] == of[i] {
            e += 1;
        }
        for s in 0..2 {
            for (vals, d) in [(&mut m, p.dm), (&mut r, p.dr)] {
                // (within dil_n segments only: the plan's end welded or trimmed in one plan set and not in another
                // moves no segment's params farther along it: Codex review of 861e550f)
                let v0: Vec<f64> = (i..e).map(|q| vals[2 * q + s]).collect();
                for q in i..e {
                    let j = 2 * q + s;
                    if fl[j] & OLD != 0 {
                        continue;
                    }
                    let mut best = v0[q - i];
                    for o in q.saturating_sub(p.dil_n).max(i)..(q + p.dil_n + 1).min(e) {
                        if fl[2 * o + s] & OLD == 0 {
                            best = best.max(v0[o - i] - d * (o as f64 - q as f64).abs());
                        }
                    }
                    vals[j] = best;
                }
            }
        }
        i = e;
    }
    // (the dilation never carries a reach past a side's own building: Codex review of ff9d2c55)
    for j in 0..n * 2 {
        r[j] = r[j].min(lim[j]);
    }
    // at each segment's ends: the mean of its and its neighbour's along the plan (both corridor sides), so the slope and
    // reach run on continuously through a vertex (a step there creased the ground along the joint's bisector)
    let mut me = vec![0.0f64; n * 4];
    let mut re = vec![0.0f64; n * 4];
    for i in 0..n {
        for s in 0..2 {
            let j = 2 * i + s;
            let (mut ma, mut mb, mut ra, mut rb) = (m[j], m[j], r[j], r[j]);
            if fl[j] & OLD == 0 {
                if i > 0 && of[i - 1] == of[i] && fl[j - 2] & OLD == 0 {
                    ma = 0.5 * (m[j - 2] + m[j]);
                    ra = 0.5 * (r[j - 2] + r[j]);
                }
                if i + 1 < n && of[i + 1] == of[i] && fl[j + 2] & OLD == 0 {
                    mb = 0.5 * (m[j] + m[j + 2]);
                    rb = 0.5 * (r[j] + r[j + 2]);
                }
            }
            me[4 * i + s] = ma;
            me[4 * i + 2 + s] = mb;
            re[4 * i + s] = ra.min(lim[j]);       // (nor the end averaging)
            re[4 * i + 2 + s] = rb.min(lim[j]);
        }
    }
    (me, re, fl)
}

/// One segment as the apply reads it.
#[derive(Clone, Copy)]
struct Seg {
    ax: f64, ay: f64, abx: f64, aby: f64, l2: f64, len: f64, za: f64, zb: f64, h: f64, rh: f64, ba: f64, bb: f64,
    width_delta: f64, banked: bool, m: [[f64; 2]; 2], r: [[f64; 2]; 2], old: [f64; 2], reach: f64, reach2: f64,  // (m, r: [side][end])
    // (per end, then side: the unit direction out of the segment its end cap measures how far behind that end a point
    // lies (cap_loosen); the neighbour's own along the plan where it goes on with that side's corridor)
    cap: [[[f64; 2]; 2]; 2],
    // (its middle and the square of a radius past which no point is within its reach: half its length + the reach,
    // with a margin far over any rounding of either distance)
    mx: f64, my: f64, rc2: f64,
}

/// The segments' arrays (grading.PlanPack's) and each side's params (m, r: (n, 4) as params gives them).
pub struct Segs<'a> {
    pub a: ArrayView2<'a, f64>, pub b: ArrayView2<'a, f64>, pub za: ArrayView1<'a, f64>, pub zb: ArrayView1<'a, f64>,
    pub width_delta: Option<ArrayView1<'a, f64>>,
    pub h: ArrayView1<'a, f64>, pub rh: ArrayView1<'a, f64>, pub ba: ArrayView1<'a, f64>,
    pub bb: ArrayView1<'a, f64>, pub m: ArrayView2<'a, f64>, pub r: ArrayView2<'a, f64>,
    pub fl: ArrayView2<'a, u8>,
    // (each segment end's continuation (segments, 2: its a's, its b's): -1 none, else 2 k + j, segment k's end j (0 its
    // a, 1 its b) lying there; PlanPack's nb, from its plans and its validated links between them, never inferred here)
    pub nb: ArrayView2<'a, i64>,
}

fn prep(s: &Segs, i: usize, p: &ApplyPrm) -> Seg {
    let (ax, ay, bx, by) = (s.a[[i, 0]], s.a[[i, 1]], s.b[[i, 0]], s.b[[i, 1]]);
    let (abx, aby) = (bx - ax, by - ay);
    let l2 = abx * abx + aby * aby;
    let old = [if s.fl[[i, 0]] & OLD != 0 { 1.0 } else { 0.0 }, if s.fl[[i, 1]] & OLD != 0 { 1.0 } else { 0.0 }];
    let r = [[s.r[[i, 0]], s.r[[i, 2]]], [s.r[[i, 1]], s.r[[i, 3]]]];
    let m = [[s.m[[i, 0]], s.m[[i, 2]]], [s.m[[i, 1]], s.m[[i, 3]]]];
    let old_r = if old[0] > 0.0 || old[1] > 0.0 { p.old_reach + p.old_fade } else { 0.0 };
    let reach = s.h[i] + r[0][0].max(r[0][1]).max(r[1][0]).max(r[1][1]).max(old_r).max(p.core_blend);
    let len = l2.sqrt();
    let (ux, uy) = if len > 1e-9 { (abx / len, aby / len) } else { (0.0, 0.0) };
    // (each end's neighbour along the plan: the segment PlanPack says it goes on into there (nb: the next segment of
    // its own plan, or the one plan its end carries straight on into); a side it carries on with is not today's there.
    // Not from the pack's order or coincident points: an unrelated plan's end there is no continuation, Codex)
    let n = s.h.len();
    let mut cap = [[[-ux, -uy]; 2], [[ux, uy]; 2]];
    #[allow(clippy::needless_range_loop)]
    for e in 0..2usize {
        let v = s.nb[[i, e]];
        if v < 0 || (v >> 1) as usize >= n {
            continue;
        }
        let (k, j) = ((v >> 1) as usize, (v & 1) as usize);
        // (away from the joint along k: from its end j to its other end; its sides as ours where it runs our way)
        let (nx, ny) = (s.b[[k, 0]] - s.a[[k, 0]], s.b[[k, 1]] - s.a[[k, 1]]);
        let nl = (nx * nx + ny * ny).sqrt();
        if nl <= 1e-9 {
            continue;
        }
        let sg = if j == 0 { 1.0 } else { -1.0 };
        let swap = j == e;
        for (side, c) in cap[e].iter_mut().enumerate() {
            let ks = if swap { 1 - side } else { side };
            if s.fl[[k, ks]] & OLD == 0 {
                *c = [sg * nx / nl, sg * ny / nl];
            }
        }
    }
    let rc = 0.5 * len + reach;
    let rc = rc + 1e-6 * (1.0 + rc);
    Seg {
        width_delta: s.width_delta.as_ref().map_or(0.0, |d| d[i]),
        ax, ay, abx, aby, l2, len, za: s.za[i], zb: s.zb[i], h: s.h[i], rh: s.rh[i], ba: s.ba[i],
        bb: s.bb[i], banked: s.ba[i] != 0.0 || s.bb[i] != 0.0, m, r, old, reach, reach2: reach * reach, cap,
        mx: ax + 0.5 * abx, my: ay + 0.5 * aby, rc2: rc * rc,
    }
}

/// A point's state over its segments: the corridors' tightest bounds and their offsets, today's weight.
#[derive(Clone, Copy)]
struct St {
    lo: f64, lo_off: f64, up: f64, up_off: f64, w: f64,
    lo_si: u32, up_si: u32,     // (the segments that gave them: a tie's earlier segment, as a pass in segment order)
}

const ST0: St = St { lo: f64::NEG_INFINITY, lo_off: 0.0, up: f64::INFINITY, up_off: 0.0, w: 0.0, lo_si: u32::MAX,
                     up_si: u32::MAX };

/// One segment's share at a point: today's weights (inside its core and core_blend past it; beside a side that is
/// today's) and, inside its corridor (cor), the corridor's bounds and their offsets.
#[derive(Clone, Copy)]
struct Pv {
    wc: f64, wo: f64, cor: bool, lo: f64, lo_off: f64, up: f64, up_off: f64,
}

#[inline(always)]
fn pair_eval(g: &Seg, x: f64, y: f64, p: &ApplyPrm) -> Option<Pv> {
    // (farther from its middle than half its length + its reach: out of its reach, decided without a division)
    let (ex, ey) = (x - g.mx, y - g.my);
    if ex * ex + ey * ey > g.rc2 {
        return None;
    }
    let (qx, qy) = (x - g.ax, y - g.ay);
    let tu = if g.l2 > 1e-12 { (qx * g.abx + qy * g.aby) / g.l2 } else { 0.0 };
    let t = tu.clamp(0.0, 1.0);
    let (dx, dy) = (qx - t * g.abx, qy - t * g.aby);
    let d2 = dx * dx + dy * dy;
    if d2 > g.reach2 {
        return None;
    }
    let cross = if g.l2 > 1e-12 { (qy * g.abx - qx * g.aby) / g.len } else { 0.0 };
    let d = d2.sqrt();
    let inset = crate::gradebins::taper_inset(g.width_delta, t);
    let x_out = d - (g.h - inset);
    let mut v = Pv { wc: 0.0, wo: 0.0, cor: false, lo: 0.0, lo_off: 0.0, up: 0.0, up_off: 0.0 };
    // inside the core and core_blend past it: today's
    if x_out < p.core_blend {
        v.wc = 1.0 - smooth(x_out / p.core_blend);
    }
    // the side (its left's share: 1 beside its left, 0 beside its right, mixed across its end caps)
    let wl = if d > 1e-12 { smooth(0.5 + 0.5 * (cross / d).clamp(-1.0, 1.0)) } else { 0.5 };
    let old = wl * g.old[0] + (1.0 - wl) * g.old[1];
    if old > 0.0 {
        v.wo = old * (1.0 - smooth((x_out - p.old_reach) / p.old_fade));
    }
    if x_out <= 0.0 || old >= 1.0 {
        return Some(v);
    }
    // the corridor round the segment's height at the foot of the perpendicular, carried on along its line past its
    // ends (up to EXT_M, at EXT_GRADE at most): taken at a clamped end, a graded road's vertices' end caps stood
    // lower (downhill) or higher than the next segment's own line beside them, and with the strip's few cm per m of
    // tolerance every vertex cut its neighbours' corridors: a braid along the edge of every road on a grade, a comb
    // down its slopes
    let (m, r) = side_mr(g, t, wl);
    if x_out >= r {
        return Some(v);
    }
    let mut z = g.za + (g.zb - g.za) * t + p.lift;
    if g.l2 > 1e-12 && tu != t {
        let over = if tu > 1.0 { (tu - 1.0) * g.len } else { tu * g.len };     // (signed: past b > 0, before a < 0)
        let grade = ((g.zb - g.za) / g.len).clamp(-EXT_GRADE, EXT_GRADE);
        z += grade * over.clamp(-EXT_M, EXT_M);
    }
    if g.banked && g.l2 > 1e-12 {   // (a banked curve: its edge's height, as gradebins' target)
        z += (g.ba + (g.bb - g.ba) * t) * cross.clamp(-(g.rh - inset), g.rh - inset);
    }
    let xs = x_out.min(p.l0);
    let mut f = m * shape(x_out - p.l0, p.crest);
    let u = (x_out - (r - p.close_t)) / p.close_t;
    if u > 0.0 {
        f += p.close_k * u * u / (1.0 - u).max(1e-9);
    }
    // (across an end cap whose other side keeps today's grading: loosened to nothing as the mix turns to that side, so
    // the corridor never stops at once where the cap meets that side, past today's fade: Codex review of ff9d2c55)
    if old > 0.0 {
        f += OLD_LOOSEN_M * old / (1.0 - old);
    }
    // (behind an end its plan does not carry its corridor on past (the plan's end, or a neighbour whose side keeps
    // today's grading: a bridge's abutment metres), loosened by how far behind: its probes read the ground beside it
    // only, and the corridor swept round its end cap cut a 14 m trench with walls behind the Stelvio's deck end at
    // Premadio and stood a 24 m fill in the valley past a plan's end, each closing at its reach as a wall)
    if tu != t {
        f += cap_loosen(g, x, y, tu > 1.0, wl);
    }
    v.lo_off = p.s_lo * xs + f;
    v.up_off = p.s_up * xs + f;
    v.lo = z - v.lo_off;
    v.up = z + v.up_off;
    v.cor = true;
    Some(v)
}

/// Segment si's share into the point's state: the same state in any pair order (max and min; of bounds equal with
/// equal offsets, which differ at most in a zero's sign, the earlier segment's, as a pass in segment order keeps).
#[inline(always)]
fn take(st: &mut St, v: &Pv, si: u32) {
    if v.wc > st.w {
        st.w = v.wc;
    }
    if v.wo > st.w {
        st.w = v.wo;
    }
    if v.cor {
        if v.lo > st.lo || (v.lo == st.lo && (v.lo_off < st.lo_off || (v.lo_off == st.lo_off && si < st.lo_si))) {
            st.lo = v.lo;
            st.lo_off = v.lo_off;
            st.lo_si = si;
        }
        if v.up < st.up || (v.up == st.up && (v.up_off < st.up_off || (v.up_off == st.up_off && si < st.up_si))) {
            st.up = v.up;
            st.up_off = v.up_off;
            st.up_si = si;
        }
    }
}

/// m: the bounds within this of a conflict's limit weigh in its offset (finish)
const CONFLICT_K: f64 = 1.0;
/// m a corridor is loosened by at a side mix half today's (old / (1 - old) times this)
const OLD_LOOSEN_M: f64 = 4.0;
/// m per m behind an end its plan does not carry its corridor on past (cap_loosen), on from ...
const CAP_K: f64 = 1.0;
/// ... a rounded start over this many metres (no crease at the end's line)
const CAP_ROUND_M: f64 = 6.0;

/// How much a segment's corridor is loosened at (x, y) in its end cap (past_b: b's, else a's): CAP_K per metre the
/// point lies behind that end (the less of behind its own end's line and) along the direction its plan goes on in (the neighbour's, where the plan goes on with that
/// side's corridor: nothing in the wedge outside a bend, where the point is past the neighbour's end too, and only
/// metres beside the neighbour, whose own corridor binds there), rounded in over CAP_ROUND_M; each side's mixed by wl
/// as side_mr mixes them. Behind the end of a plan, or of a run that a neighbour's today's side ends, every segment's
/// cap is loosened at least as much as the last one's: the ground there rises (falls) to the natural at m + CAP_K at
/// most instead of closing at the reach.
#[inline(always)]
fn cap_loosen(g: &Seg, x: f64, y: f64, past_b: bool, wl: f64) -> f64 {
    let (e, ox, oy) = if past_b { (1, g.ax + g.abx, g.ay + g.aby) } else { (0, g.ax, g.ay) };
    let (qx, qy) = (x - ox, y - oy);
    // (and no more than behind its own end's line: nothing on that line, the corridor beside the segment carried on
    // continuously into its cap)
    let sg = if past_b { 1.0 } else { -1.0 };
    let own = if g.len > 1e-9 { sg * (qx * g.abx + qy * g.aby) / g.len } else { 0.0 };
    let side = |s: usize| shape((qx * g.cap[e][s][0] + qy * g.cap[e][s][1]).min(own).max(0.0), CAP_ROUND_M);
    let l = if g.old[0] > 0.0 {
        side(1)
    } else if g.old[1] > 0.0 {
        side(0)
    } else {
        wl * side(0) + (1.0 - wl) * side(1)
    };
    CAP_K * l
}

/// m a segment's line is carried on past its ends (the corridor's height there) ...
const EXT_M: f64 = 4.0;
/// ... at this grade at most (gradebins' EXTRAP_MAX_GRADE: a few cm long segment holding a step)
const EXT_GRADE: f64 = crate::gradebins::EXTRAP_MAX_GRADE;

/// A side's slope and reach at t along the segment (each side's from its value at a to its value at b), the sides
/// mixed by wl (the left's share); a side that is today's mixes in nothing of it.
#[inline(always)]
fn side_mr(g: &Seg, t: f64, wl: f64) -> (f64, f64) {
    let ml = g.m[0][0] + (g.m[0][1] - g.m[0][0]) * t;
    let mr = g.m[1][0] + (g.m[1][1] - g.m[1][0]) * t;
    let rl = g.r[0][0] + (g.r[0][1] - g.r[0][0]) * t;
    let rr = g.r[1][0] + (g.r[1][1] - g.r[1][0]) * t;
    if g.old[0] > 0.0 {
        (mr, rr)
    } else if g.old[1] > 0.0 {
        (ml, rl)
    } else {
        (wl * ml + (1.0 - wl) * mr, wl * rl + (1.0 - wl) * rr)
    }
}

/// polynomial smooth min of a, b over k (k <= 0: min)
#[inline(always)]
fn smin(a: f64, b: f64, k: f64) -> f64 {
    if k <= 0.0 {
        return a.min(b);
    }
    let hh = 0.5 + 0.5 * (b - a) / k;
    if hh >= 1.0 {
        return a;           // (exactly: a point past the blend keeps its own height to the bit)
    }
    if hh <= 0.0 {
        return b;
    }
    b + (a - b) * hh - k * hh * (1.0 - hh)
}

/// The corridors' two corridors disagree (finish's third case).
#[inline(always)]
fn conflict(st: &St) -> bool {
    !(st.lo == f64::NEG_INFINITY && st.up == f64::INFINITY) && !(st.lo <= st.up)
}

/// The height without today's weight where the corridors agree (not conflict(st)): the natural base clamped to
/// [lo, up], smoothly at the daylight line.
#[inline(always)]
fn finish_agree(st: &St, base: f64, p: &ApplyPrm) -> f64 {
    if st.lo == f64::NEG_INFINITY && st.up == f64::INFINITY {
        return base;
    }
    let mut v = base;
    if st.lo != f64::NEG_INFINITY {
        v = -smin(-v, -st.lo, p.smooth_k);
    }
    if st.up != f64::INFINITY {
        v = smin(v, st.up, p.smooth_k);
    }
    v.clamp(st.lo, st.up)
}

/// The height without today's weight where two corridors disagree (conflict(st), st over every pair of the point's
/// list): between the binding limits by their offsets, each side's offset the mean of the bounds within CONFLICT_K of
/// its limit, weighted by how near (a bound taking over from another changes it smoothly; the binding one's alone
/// jumped there: Codex review of 861e550f). Summed in the pairs' order (the plan set's segment order: the same for the
/// same segments in every set), each pair's bounds evaluated again (a pure function of the pair).
fn finish_conflict(st: &St, segs: &[Seg], list: &[u32], x: f64, y: f64, p: &ApplyPrm) -> f64 {
    let (mut lsw, mut lso, mut usw, mut uso) = (0.0f64, 0.0f64, 0.0f64, 0.0f64);
    let acc = |sw: &mut f64, so: &mut f64, b: f64, off: f64, lim: f64, sign: f64| {
        let u = 1.0 - sign * (lim - b) / CONFLICT_K;
        if u > 0.0 {
            let w = u * u;
            *sw += w;
            *so += w * off;
        }
    };
    for &si in list {
        if let Some(v) = pair_eval(&segs[si as usize], x, y, p) {
            if v.cor {
                acc(&mut lsw, &mut lso, v.lo, v.lo_off, st.lo, 1.0);
                acc(&mut usw, &mut uso, v.up, v.up_off, st.up, -1.0);
            }
        }
    }
    let lo_off = if lsw > 0.0 { lso / lsw } else { 0.0 };
    let up_off = if usw > 0.0 { uso / usw } else { 0.0 };
    let s = lo_off + up_off;
    if s > 1e-12 { (up_off * st.lo + lo_off * st.up) / s } else { 0.5 * (st.lo + st.up) }
}

const BIN_POINTS_PER_THREAD: usize = 4096;

/// The heights at the points (px, py) without today's weight (v) and today's weight (w, 1: today's g0 alone): the
/// natural base where no corridor binds, the corridor's where the segments' sides are not today's. The height is
/// v + w (g0 - v) (combine). Each point meets every segment within its reach (nothing depends on the other points of
/// the call). A point with a coordinate not finite: w 1.
///
/// A point meets its bin's segments nearest first (from the bin's centre, past their cores): the state is order free
/// (take), and a point in a core or beside a side that keeps today's grading reaches weight 1 within its first pairs
/// and stops there. A conflict's sums run in the segments' order (finish_conflict).
pub fn apply_vw(px: ArrayView1<f64>, py: ArrayView1<f64>, base: ArrayView1<f64>, s: &Segs, p: &ApplyPrm)
                -> (Vec<f64>, Vec<f64>) {
    let n = px.len();
    let mut vo: Vec<f64> = base.iter().copied().collect();
    let mut wo = vec![1.0f64; n];
    if n == 0 || s.h.is_empty() {
        return (vo, wo);
    }
    let segs: Vec<Seg> = (0..s.h.len()).map(|i| prep(s, i, p)).collect();
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        if px[i].is_finite() && py[i].is_finite() {
            x0 = x0.min(px[i]); x1 = x1.max(px[i]);
            y0 = y0.min(py[i]); y1 = y1.max(py[i]);
        }
    }
    if !(x0 <= x1 && y0 <= y1) {
        return (vo, wo);
    }
    let (wx, wy) = (x1 - x0, y1 - y0);
    let mut fb = p.bin_m.max(0.5);
    let cap = (2 * n + 64) as f64;
    while ((wx / fb).floor() + 1.0) * ((wy / fb).floor() + 1.0) > cap {
        fb *= 1.5;
    }
    let fnx = (wx / fb).floor() as usize + 1;
    let fny = (wy / fb).floor() as usize + 1;
    let nb = fnx * fny;
    // the points by bin (counting sort; a NaN coordinate: no bin, w 1)
    let mut pb: Vec<u32> = vec![u32::MAX; n];
    let mut pstart = vec![0u32; nb + 1];
    for i in 0..n {
        if !(px[i].is_finite() && py[i].is_finite()) {
            continue;
        }
        let ix = (((px[i] - x0) / fb).floor().max(0.0) as usize).min(fnx - 1);
        let iy = (((py[i] - y0) / fb).floor().max(0.0) as usize).min(fny - 1);
        pb[i] = (iy * fnx + ix) as u32;
        pstart[iy * fnx + ix + 1] += 1;
    }
    for k in 0..nb {
        pstart[k + 1] += pstart[k];
    }
    let mut fill = pstart.clone();
    let mut porder = vec![0u32; pstart[nb] as usize];
    for i in 0..n {
        if pb[i] != u32::MAX {
            let b = pb[i] as usize;
            porder[fill[b] as usize] = i as u32;
            fill[b] += 1;
        }
    }
    // each bin's segments (every segment whose reach can touch the bin), with their centre's distance past the core
    let half_diag = fb * std::f64::consts::FRAC_1_SQRT_2;
    let mut found: Vec<(u32, u32, f64)> = Vec::new();
    for (si, g) in segs.iter().enumerate() {
        let rr = g.reach + 1e-6;
        let (lx, hx) = (g.ax.min(g.ax + g.abx) - rr, g.ax.max(g.ax + g.abx) + rr);
        let (ly, hy) = (g.ay.min(g.ay + g.aby) - rr, g.ay.max(g.ay + g.aby) + rr);
        if hx < x0 || lx > x1 || hy < y0 || ly > y1 {
            continue;
        }
        let bx0 = ((lx - x0) / fb).floor().max(0.0) as usize;
        let bx1 = (((hx - x0) / fb).floor().max(0.0) as usize).min(fnx - 1);
        let by0 = ((ly - y0) / fb).floor().max(0.0) as usize;
        let by1 = (((hy - y0) / fb).floor().max(0.0) as usize).min(fny - 1);
        let lim = rr + half_diag;
        for by in by0..=by1 {
            let cy = y0 + (by as f64 + 0.5) * fb;
            for bx in bx0..=bx1 {
                let b = by * fnx + bx;
                if pstart[b + 1] == pstart[b] {
                    continue;
                }
                let cx = x0 + (bx as f64 + 0.5) * fb;
                // (the bin's centre within the reach + its half diagonal)
                let (qx, qy) = (cx - g.ax, cy - g.ay);
                let t = if g.l2 > 1e-12 { ((qx * g.abx + qy * g.aby) / g.l2).clamp(0.0, 1.0) } else { 0.0 };
                let (dx, dy) = (qx - t * g.abx, qy - t * g.aby);
                let dc = (dx * dx + dy * dy).sqrt();
                if dc <= lim {
                    found.push((b as u32, si as u32, dc - g.h));
                }
            }
        }
    }
    // (in segment order within each bin: the counting sort is stable); nearest first: a copy sorted by the distance
    // past the core, then the segment (a total order: the same in every run)
    let mut start = vec![0u32; nb + 1];
    for &(b, _, _) in &found {
        start[b as usize + 1] += 1;
    }
    for k in 0..nb {
        start[k + 1] += start[k];
    }
    let mut items = vec![0u32; found.len()];
    let mut near = vec![(0f64, 0u32); found.len()];
    let mut at = start.clone();
    for &(b, si, dx) in &found {
        items[at[b as usize] as usize] = si;
        near[at[b as usize] as usize] = (dx, si);
        at[b as usize] += 1;
    }
    for b in 0..nb {
        near[start[b] as usize..start[b + 1] as usize].sort_unstable_by(|u, v| u.0.total_cmp(&v.0).then(u.1.cmp(&v.1)));
    }
    let nitems: Vec<u32> = near.iter().map(|v| v.1).collect();
    drop(near);
    drop(found);
    let np_ = porder.len();
    let mut res = vec![[0f64; 2]; np_];
    let run = |list: &[u32], x: f64, y: f64| -> St {
        let mut st = ST0;
        for &si in list {
            if let Some(v) = pair_eval(&segs[si as usize], x, y, p) {
                take(&mut st, &v, si);
                if st.w >= 1.0 {
                    break;      // (today's height stands whatever the rest: the same result, fewer pairs)
                }
            }
        }
        st
    };
    let work = |b0: usize, b1: usize, r: &mut [[f64; 2]]| {
        let off = pstart[b0] as usize;
        for b in b0..b1 {
            let list = &items[start[b] as usize..start[b + 1] as usize];
            let nlist = &nitems[start[b] as usize..start[b + 1] as usize];
            for k in pstart[b] as usize..pstart[b + 1] as usize {
                let i = porder[k] as usize;
                let (x, y) = (px[i], py[i]);
                let st = run(nlist, x, y);
                r[k - off] = if st.w >= 1.0 {
                    [base[i], 1.0]
                } else if conflict(&st) {
                    [finish_conflict(&st, &segs, list, x, y, p), st.w]
                } else {
                    [finish_agree(&st, base[i], p), st.w]
                };
            }
        }
    };
    let threads = crate::par::threads_for(np_, BIN_POINTS_PER_THREAD);
    if threads <= 1 {
        work(0, nb, &mut res);
    } else {
        let want = (threads * 8).min(nb);
        let per = np_.div_ceil(want).max(1);
        let mut parts: Vec<(usize, usize, &mut [[f64; 2]])> = Vec::with_capacity(want + 1);
        let mut rest: &mut [[f64; 2]] = &mut res;
        let mut b0 = 0usize;
        while b0 < nb {
            let mut b1 = b0 + 1;
            while b1 < nb && ((pstart[b1] - pstart[b0]) as usize) < per {
                b1 += 1;
            }
            let len = (pstart[b1] - pstart[b0]) as usize;
            let (head, tail) = std::mem::take(&mut rest).split_at_mut(len);
            parts.push((b0, b1, head));
            rest = tail;
            b0 = b1;
        }
        crate::par::run_parts(parts, threads, |(b0, b1, r)| work(b0, b1, r));
    }
    for (k, &i) in porder.iter().enumerate() {
        vo[i as usize] = res[k][0];
        wo[i as usize] = res[k][1];
    }
    (vo, wo)
}

/// The height from apply_vw's v and w and today's g0.
#[inline(always)]
pub fn combine(v: f64, w: f64, g0: f64) -> f64 {
    if w >= 1.0 { g0 } else if w <= 0.0 { v } else { v + w * (g0 - v) }
}

/// The heights at the points (px, py): natural base, today's graded g0, the corridor's where the segments' sides are
/// not today's (apply_vw combined with g0).
pub fn apply(px: ArrayView1<f64>, py: ArrayView1<f64>, base: ArrayView1<f64>, g0: ArrayView1<f64>, s: &Segs,
             p: &ApplyPrm) -> Vec<f64> {
    let (v, w) = apply_vw(px, py, base, s, p);
    (0..px.len()).map(|i| combine(v[i], w[i], g0[i])).collect()
}


/// The segments needed for a query's batter parameters, in original pack order.
/// A point can only see a corridor within half width + reach of its segment. Keep
/// whole plans for the along-plan closing, opening and dilation. Each candidate's
/// end caps also read the OLD flags on its two linked segments, even across plans.
#[pyo3::pyfunction]
pub fn batter_window<'py>(
    py: pyo3::Python<'py>,
    a: numpy::PyReadonlyArray2<'py, f64>, b: numpy::PyReadonlyArray2<'py, f64>,
    h: numpy::PyReadonlyArray1<'py, f64>, of: numpy::PyReadonlyArray1<'py, i64>,
    nb: numpy::PyReadonlyArray2<'py, i64>, bounds: (f64, f64, f64, f64), reach: f64,
) -> pyo3::PyResult<(pyo3::Bound<'py, numpy::PyArray1<i64>>, pyo3::Bound<'py, numpy::PyArray2<i64>>)> {
    use numpy::{ndarray::Array2, IntoPyArray};
    use pyo3::exceptions::PyValueError;
    let (a, b, h, of, nb) = (a.as_array(), b.as_array(), h.as_array(), of.as_array(), nb.as_array());
    let n = h.len();
    if a.dim() != (n, 2) || b.dim() != (n, 2) || nb.dim() != (n, 2) || of.len() != n
        || of.iter().any(|&p| p < 0 || p as usize >= n)
        || nb.iter().any(|&e| e < -1 || e >= 2 * n as i64) {
        return Err(PyValueError::new_err("batter_window: invalid segment arrays or links"));
    }
    let mut keep_plan = vec![false; n];
    for i in 0..n {
        let pad = h[i] + reach + 1e-6; // same outward margin as apply_vw's bin search
        if a[[i, 0]].max(b[[i, 0]]) + pad < bounds.0 || a[[i, 0]].min(b[[i, 0]]) - pad > bounds.2
            || a[[i, 1]].max(b[[i, 1]]) + pad < bounds.1 || a[[i, 1]].min(b[[i, 1]]) - pad > bounds.3 {
            continue;
        }
        keep_plan[of[i] as usize] = true;
        for end in nb.row(i) {
            if *end >= 0 {
                keep_plan[of[(*end / 2) as usize] as usize] = true;
            }
        }
    }
    let mut ids = Vec::new();
    let mut local = vec![-1i64; n];
    for i in 0..n {
        if keep_plan[of[i] as usize] {
            local[i] = ids.len() as i64;
            ids.push(i as i64);
        }
    }
    let links = Array2::from_shape_fn((ids.len(), 2), |(i, e)| {
        let end = nb[[ids[i] as usize, e]];
        if end < 0 || local[(end / 2) as usize] < 0 { -1 } else { 2 * local[(end / 2) as usize] + end % 2 }
    });
    Ok((ids.into_pyarray(py), links.into_pyarray(py)))
}
