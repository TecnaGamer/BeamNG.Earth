//! The stands' lead-ins (server/standpaths.py build, its docstring the spec): the lanes first (the mapped centrelines and a
//! spine behind each row of stands no lane reaches), then each stand's lens off the first lane its reversed axis meets.
//!
//! A port of the Python as it is, step for step and in its evaluation order, so the two agree to the bit (the user,
//! 2026-11-17: "directly port standpaths.build whole"): numpy's float64 arithmetic in the same order, its pairwise sum for
//! a 1-D `.sum()` (`pysum`), Python's `round(x, 6)` in the sort keys (`round6`), Python's float `%` (`pymod`), the
//! transcendental functions from dm.rs (dmath / dnp: the libm crate's), and the even-odd tests of the Python's own
//! (parkedaircraft._pip in `pip`, polygon.contains_points in `crate::contains_points_vec`). The constants come from the
//! caller (taxiwaymarks' and standpaths' own: one place for each), in `Params`.

use crate::dm::Dm;

type P = [f64; 2];

const EPS: f64 = 1e-9;

#[inline]
fn sub(a: P, b: P) -> P { [a[0] - b[0], a[1] - b[1]] }
#[inline]
fn add(a: P, b: P) -> P { [a[0] + b[0], a[1] + b[1]] }
#[inline]
fn mul(a: P, s: f64) -> P { [a[0] * s, a[1] * s] }
#[inline]
fn div(a: P, s: f64) -> P { [a[0] / s, a[1] / s] }
#[inline]
fn neg(a: P) -> P { [-a[0], -a[1]] }
#[inline]
fn hyp(a: P) -> f64 { a[0].dhypot(a[1]) }
#[inline]
fn dot(a: P, b: P) -> f64 { a[0] * b[0] + a[1] * b[1] }
#[inline]
fn clip(x: f64, lo: f64, hi: f64) -> f64 { if x < lo { lo } else if x > hi { hi } else { x } }
#[inline]
fn fmax(a: f64, b: f64) -> f64 { if b > a { b } else { a } }      // (Python's max(a, b): the first of equals)
#[inline]
fn fmin(a: f64, b: f64) -> f64 { if b < a { b } else { a } }

/// numpy's float64 `add.reduce` of a contiguous 1-D array (pairwise_sum: 8 accumulators per block of 128, halves above).
pub(crate) fn pysum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut r = 0.0;
        for &x in a { r += x; }
        return r;
    }
    if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - n % 8 {
            for j in 0..8 { r[j] += a[i + j]; }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n { res += a[i]; i += 1; }
        return res;
    }
    let mut n2 = n / 2;
    n2 -= n2 % 8;
    pysum(&a[..n2]) + pysum(&a[n2..])
}

/// Python's round(x, 6) (the correctly rounded 6-place decimal, read back): the sort keys' rounding.
fn round6(x: f64) -> f64 {
    if !x.is_finite() { return x; }
    format!("{:.6}", x).parse::<f64>().unwrap_or(x)
}

/// Python's float x % y (the sign of y).
pub(crate) fn pymod(x: f64, y: f64) -> f64 {
    let m = x % y;
    if m != 0.0 && ((y < 0.0) != (m < 0.0)) { m + y } else { m }
}

/// The constants (standpaths.py and taxiwaymarks.py: the caller passes them, `params()` there names the order).
pub struct Params {
    pub lens_r_k: f64, pub lens_r_max_k: f64, pub lens_room_k: f64, pub reach_spans: f64,
    pub turn_max_rad: f64, pub slit_turn_rad: f64, pub min_sin: f64, pub slit_near_m: f64, pub slit_cos: f64,
    pub chain_m: f64, pub chain_cos: f64, pub wing_margin_m: f64, pub conn_tries: usize, pub conn_step_m: f64,
    pub conn_max_m: f64, pub fan_cos: f64, pub paved_step_m: f64, pub off_paved: f64, pub trim_tol_m: f64,
    pub free_on_m: f64, pub free_edge_m: f64, pub free_prefer_k: f64, pub free_prefer_m: f64, pub free_reach_m: f64,
    pub row_ring_min: usize, pub row_ring_mean: f64,
    // taxiwaymarks'
    pub spine_link_m: f64, pub spine_link_cos: f64, pub spine_clear_m: f64, pub lead_stub_m: f64,
    pub min_radius_m: f64, pub arc_step_rad: f64, pub dubins_turn_max: f64, pub pad_edge_m: f64, pub sample_m: f64,
    // the tuples
    pub lens_tries: Vec<(f64, f64)>, pub radius_steps: Vec<f64>, pub slit_back_m: Vec<f64>, pub spine_back_steps: Vec<f64>,
}

impl Params {
    pub fn from(v: &[f64], tries: &[f64], radius_steps: &[f64], slit_back: &[f64], back_steps: &[f64]) -> Option<Params> {
        if v.len() != 35 || tries.len() % 2 != 0 { return None; }
        Some(Params {
            lens_r_k: v[0], lens_r_max_k: v[1], lens_room_k: v[2], reach_spans: v[3], turn_max_rad: v[4],
            slit_turn_rad: v[5], min_sin: v[6], slit_near_m: v[7], slit_cos: v[8], chain_m: v[9], chain_cos: v[10],
            wing_margin_m: v[11], conn_tries: v[12] as usize, conn_step_m: v[13], conn_max_m: v[14], fan_cos: v[15],
            paved_step_m: v[16], off_paved: v[17], trim_tol_m: v[18], free_on_m: v[19], free_edge_m: v[20],
            free_prefer_k: v[21], free_prefer_m: v[22], free_reach_m: v[23], row_ring_min: v[24] as usize,
            row_ring_mean: v[25], spine_link_m: v[26], spine_link_cos: v[27], spine_clear_m: v[28], lead_stub_m: v[29],
            min_radius_m: v[30], arc_step_rad: v[31], dubins_turn_max: v[32], pad_edge_m: v[33], sample_m: v[34],
            lens_tries: tries.chunks(2).map(|c| (c[0], c[1])).collect(),
            radius_steps: radius_steps.to_vec(), slit_back_m: slit_back.to_vec(), spine_back_steps: back_steps.to_vec(),
        })
    }
}

/// A stand: nose, unit direction, length, span, its final straight (taxiwaymarks.lead_straight), its smallest radius.
#[derive(Clone, Copy)]
pub struct Item { pub nose: P, pub u: P, pub length: f64, pub span: f64, pub straight: f64, pub rmin: f64 }

/// A building: its ring and its box (x0, y0, x1, y1).
pub struct Block { pub ring: Vec<P>, pub bx: [f64; 4] }

/// The runways' pads (taxiwaymarks._runway_pads): segments a -> b, half widths, way indices.
pub struct Pads { pub a: Vec<P>, pub b: Vec<P>, pub half: Vec<f64>, pub way: Vec<i64> }

pub struct Out {
    pub paint: Vec<(Vec<P>, u8, f64)>,      // (polyline, 0 spine | 1 lead, rmin)
    pub found: Vec<Option<[P; 2]>>,
    pub rows: Vec<(Vec<usize>, bool)>,      // (members, ring)
}

// ------------------------------------------------------------------------------------------------ geometry helpers

fn cumulative(p: &[P]) -> Vec<f64> {
    let mut c = Vec::with_capacity(p.len());
    c.push(0.0);
    let mut s = 0.0;
    for i in 1..p.len() {
        s += hyp(sub(p[i], p[i - 1]));
        c.push(s);
    }
    c
}

/// np.searchsorted(cum, x, side="right")
fn search_right(cum: &[f64], x: f64) -> usize { cum.partition_point(|&c| c <= x) }

fn length(p: &[P]) -> f64 {
    let d: Vec<f64> = (1..p.len()).map(|i| hyp(sub(p[i], p[i - 1]))).collect();
    pysum(&d)
}

/// standmarks.at: (point, unit direction) a distance s along the polyline (clamped to its ends).
fn at_poly(p: &[P], s: f64) -> (P, P) {
    let cum = cumulative(p);
    let s = fmin(fmax(s, 0.0), cum[cum.len() - 1]);
    let mut j = (search_right(&cum, s) as i64 - 1).min(p.len() as i64 - 2);
    j = j.max(0);
    let j = j as usize;
    let d = sub(p[j + 1], p[j]);
    let ln = fmax(cum[j + 1] - cum[j], EPS);
    (add(p[j], mul(d, (s - cum[j]) / ln)), div(d, ln))
}

/// taxiwaymarks._subpath
fn subpath(p: &[P], lo: f64, hi: f64) -> Vec<P> {
    let cum = cumulative(p);
    let mut out = vec![at_poly(p, lo).0];
    for i in 1..p.len().saturating_sub(1) {
        if lo + 1e-6 < cum[i] && cum[i] < hi - 1e-6 { out.push(p[i]); }
    }
    out.push(at_poly(p, hi).0);
    out
}

/// taxiwaymarks._project's distance: the polyline's nearest point to p, how far.
fn project_dist(p: &[P], q: P) -> f64 {
    let mut best = f64::INFINITY;
    for i in 0..p.len() - 1 {
        let a = p[i];
        let d = sub(p[i + 1], a);
        let ln2 = fmax(dot(d, d), 1e-12);
        let t = clip(dot(sub(q, a), d) / ln2, 0.0, 1.0);
        let r = add(a, mul(d, t));
        let dist = hyp(sub(r, q));
        if dist < best { best = dist; }           // (np.argmin: the first of equals)
    }
    best
}

/// taxiwaymarks._station
fn station(p: &[P], q: P) -> f64 {
    let n = p.len() - 1;
    let mut ln = Vec::with_capacity(n);
    let (mut bk, mut bd, mut bt) = (0usize, f64::INFINITY, 0.0);
    for i in 0..n {
        let d = sub(p[i + 1], p[i]);
        let l = d[0].dhypot(d[1]);
        ln.push(l);
        let t = clip(dot(sub(q, p[i]), d) / fmax(l * l, 1e-12), 0.0, 1.0);
        let r = add(p[i], mul(d, t));
        let dist = hyp(sub(r, q));
        if dist < bd { bd = dist; bk = i; bt = t; }
    }
    pysum(&ln[..bk]) + bt * ln[bk]
}

/// taxiwaymarks._pts_dist of one point: the distance to the nearest segment.
fn pt_dist(q: P, segs: &[(P, P)]) -> f64 {
    let mut best = f64::INFINITY;
    for &(a, b) in segs {
        let ab = sub(b, a);
        let ap = sub(q, a);
        let t = clip(dot(ap, ab) / fmax(dot(ab, ab), 1e-12), 0.0, 1.0);
        let d = sub(ap, mul(ab, t));
        let v = (d[0] * d[0] + d[1] * d[1]).sqrt();
        if v < best { best = v; }
    }
    best
}

/// The polyline's points every `step` (and its end): numpy's P[j] + seg[j] * (arange(c) / c), c = max(1, ceil(len / step)).
fn samples(p: &[P], step: f64) -> Vec<P> {
    let mut out = Vec::new();
    for j in 0..p.len() - 1 {
        let seg = sub(p[j + 1], p[j]);
        let c = fmax(1.0, (hyp(seg) / step).ceil()) as i64;
        for k in 0..c {
            out.push(add(p[j], mul(seg, k as f64 / c as f64)));
        }
    }
    out.push(p[p.len() - 1]);
    out
}

/// parkedaircraft._pip: which points lie inside the ring (even-odd).
fn pip_any(ring: &[P], pts: &[P]) -> bool {
    let n = ring.len();
    for &q in pts {
        let (x, y) = (q[0], q[1]);
        let mut c = 0u32;
        for i in 0..n {
            let (a, b) = (ring[i], ring[(i + 1) % n]);
            let cross = (a[1] <= y) != (b[1] <= y);
            let dy = if b[1] == a[1] { 1.0 } else { b[1] - a[1] };
            let xi = a[0] + (y - a[1]) * (b[0] - a[0]) / dy;
            if cross && x < xi { c += 1; }
        }
        if c % 2 == 1 { return true; }
    }
    false
}

/// taxiwaymarks._crossing: a segment of one polyline strictly across one of the other.
fn crossing(a: &[P], b: &[P], tol: f64) -> bool {
    let cr = |u: P, v: P| u[0] * v[1] - u[1] * v[0];
    let split = |x: f64, y: f64| (x > tol && y < -tol) || (x < -tol && y > tol);
    for i in 0..a.len().saturating_sub(1) {
        let (p, q) = (a[i], a[i + 1]);
        let d1 = sub(q, p);
        let n1 = fmax(hyp(d1), EPS);
        for j in 0..b.len().saturating_sub(1) {
            let (r, w) = (b[j], b[j + 1]);
            let d2 = sub(w, r);
            let n2 = fmax(hyp(d2), EPS);
            let (o1, o2) = (cr(d1, sub(r, p)) / n1, cr(d1, sub(w, p)) / n1);
            let (o3, o4) = (cr(d2, sub(p, r)) / n2, cr(d2, sub(q, r)) / n2);
            if split(o1, o2) && split(o3, o4) { return true; }
        }
    }
    false
}

fn bounds(p: &[P]) -> (P, P) {
    let (mut lo, mut hi) = (p[0], p[0]);
    for q in &p[1..] {
        lo = [fmin(lo[0], q[0]), fmin(lo[1], q[1])];
        hi = [fmax(hi[0], q[0]), fmax(hi[1], q[1])];
    }
    (lo, hi)
}

/// standpaths._through: does the polyline cross or enter a building?
fn through(path: &[P], blocks: &[Block]) -> bool {
    let (lo, hi) = bounds(path);
    for bl in blocks {
        let [x0, y0, x1, y1] = bl.bx;
        if x1 < lo[0] || x0 > hi[0] || y1 < lo[1] || y0 > hi[1] { continue; }
        let mut closed = bl.ring.clone();
        closed.push(bl.ring[0]);
        if pip_any(&bl.ring, path) || crossing(path, &closed, 0.0) { return true; }
    }
    false
}

/// standpaths._off_paving
fn off_paving(path: &[P], paved: &[Vec<P>], prm: &Params) -> bool {
    if paved.is_empty() { return false; }
    let pts = samples(path, prm.paved_step_m);
    let mut inside = vec![false; pts.len()];
    let (lo, hi) = bounds(&pts);
    for r in paved {
        let (rlo, rhi) = bounds(r);
        if rhi[0] < lo[0] || rhi[1] < lo[1] || rlo[0] > hi[0] || rlo[1] > hi[1] { continue; }
        let idx: Vec<usize> = (0..pts.len()).filter(|&k| !inside[k] && pts[k][0] >= rlo[0] && pts[k][1] >= rlo[1]
                                                      && pts[k][0] <= rhi[0] && pts[k][1] <= rhi[1]).collect();
        if idx.is_empty() { continue; }
        let sel: Vec<P> = idx.iter().map(|&k| pts[k]).collect();
        let got = crate::contains_points_vec(r, &sel);
        for (n, &k) in idx.iter().enumerate() {
            if got[n] { inside[k] = true; }
        }
    }
    let off = inside.iter().filter(|&&v| !v).count();
    (off as f64) / (pts.len() as f64) > prm.off_paved
}

/// taxiwaymarks._crosses: does the polyline enter (by more than PAD_EDGE_M) a runway's pad other than the way `own`'s?
fn crosses(pads: &Pads, pts: &[P], own: i64, prm: &Params) -> bool {
    if pads.a.is_empty() { return false; }
    let (lo, hi) = bounds(pts);
    let keep: Vec<usize> = (0..pads.a.len()).filter(|&j| {
        let (a, b, h) = (pads.a[j], pads.b[j], pads.half[j]);
        fmax(a[0], b[0]) >= lo[0] - h && fmax(a[1], b[1]) >= lo[1] - h
            && fmin(a[0], b[0]) <= hi[0] + h && fmin(a[1], b[1]) <= hi[1] + h && pads.way[j] != own
    }).collect();
    if keep.is_empty() { return false; }
    let smp = samples(pts, prm.sample_m);
    for &q in &smp {
        for &j in &keep {
            let a = pads.a[j];
            let ab = sub(pads.b[j], a);
            let ap = sub(q, a);
            let t = clip(dot(ap, ab) / fmax(dot(ab, ab), 1e-12), 0.0, 1.0);
            let d = sub(ap, mul(ab, t));
            if d[0].dhypot(d[1]) < pads.half[j] - prm.pad_edge_m { return true; }
        }
    }
    false
}

/// taxiwaymarks._under_aircraft
fn under_aircraft(pts: &[P], items: &[(P, P, f64, f64)], margin: f64) -> bool {
    let smp = samples(pts, 2.0);
    for &(nose, u, length, span) in items {
        let c = sub(nose, mul(u, length / 2.0));
        for &q in &smp {
            let v = sub(q, c);
            let along = (v[0] * u[0] + v[1] * u[1]).abs() - length / 2.0;
            let across = (v[0] * -u[1] + v[1] * u[0]).abs() - span / 2.0;
            if fmax(along, 0.0).dhypot(fmax(across, 0.0)) < margin { return true; }
        }
    }
    false
}

/// The clearance test standpaths._clear makes: off the runways' pads, the other aircraft, the buildings; on the paving.
struct Clear<'a> {
    pads: &'a Pads,
    others: Vec<(P, P, f64, f64)>,
    blocks: &'a [Block],
    paved: &'a [Vec<P>],
}

impl<'a> Clear<'a> {
    fn ok(&self, path: &[P], own: i64, prm: &Params) -> bool {
        !crosses(self.pads, path, own, prm) && !through(path, self.blocks)
            && !under_aircraft(path, &self.others, prm.wing_margin_m) && !off_paving(path, self.paved, prm)
    }
}

// -------------------------------------------------------------------------------------------------------- curves

/// taxiwaymarks._arc_pts
fn arc_pts(c: P, p0: P, sense: f64, angle: f64, prm: &Params) -> Vec<P> {
    let v = sub(p0, c);
    let m = fmax(1.0, (angle / prm.arc_step_rad).ceil()) as i64;
    (0..=m).map(|k| {
        let a = sense * angle * (k as f64) / (m as f64);
        let (ca, sa) = (a.dcos(), a.dsin());
        add(c, [ca * v[0] - sa * v[1], sa * v[0] + ca * v[1]])
    }).collect()
}

/// taxiwaymarks._fillet
fn fillet(x: P, tin: P, tout: P, tl: f64, prm: &Params) -> Vec<P> {
    let cross = tin[0] * tout[1] - tin[1] * tout[0];
    let ang = cross.abs().datan2(tin[0] * tout[0] + tin[1] * tout[1]);
    let (p0, p2) = (sub(x, mul(tin, tl)), add(x, mul(tout, tl)));
    if ang < 1e-6 || tl < 1e-9 { return vec![p0, p2]; }
    let radius = tl / (ang / 2.0).dtan();
    let sense = if cross > 0.0 { 1.0 } else { -1.0 };
    let c = add(p0, mul([-tin[1], tin[0]], sense * radius));
    let mut pts = arc_pts(c, p0, sense, ang, prm);
    let n = pts.len();
    pts[n - 1] = p2;
    pts
}

/// taxiwaymarks._round_arcs
fn round_arcs(pts: &[P], radius: f64, prm: &Params) -> Vec<P> {
    let mut p: Vec<P> = pts.to_vec();
    let mut closed = false;
    if p.len() > 2 && p[0] == p[p.len() - 1] {
        closed = true;
        p.pop();
    }
    let n = p.len();
    if n < 3 { return p; }
    let mut out: Vec<P> = if closed { vec![] } else { vec![p[0]] };
    let range: Vec<usize> = if closed { (0..n).collect() } else { (1..n - 1).collect() };
    for i in range {
        let (p0, p1, p2) = (p[(i + n - 1) % n], p[i], p[(i + 1) % n]);
        let (d0, d2) = (hyp(sub(p1, p0)), hyp(sub(p2, p1)));
        if d0 < 1e-9 || d2 < 1e-9 { out.push(p1); continue; }
        let (tin, tout) = (div(sub(p1, p0), d0), div(sub(p2, p1), d2));
        let ang = (tin[0] * tout[1] - tin[1] * tout[0]).abs().datan2(tin[0] * tout[0] + tin[1] * tout[1]);
        if ang < 0.02 { out.push(p1); continue; }
        let tl = fmin(fmin(radius * (ang / 2.0).dtan(), d0 / 2.0), d2 / 2.0);
        out.extend(fillet(p1, tin, tout, tl, prm));
    }
    out.push(if closed { out[0] } else { p[n - 1] });
    out
}

/// A Dubins plan (taxiwaymarks._dubins): (length, p0, c0, s0, t0, c1, s1, t1, p1, a, straight).
#[derive(Clone, Copy)]
struct Plan { length: f64, p0: P, c0: P, s0: f64, t0: f64, c1: P, s1: f64, t1: f64, p1: P, a: f64, straight: f64 }

fn dubins(p0: P, h0: P, p1: P, h1: P, radius: f64, prm: &Params) -> Option<Plan> {
    let tau = std::f64::consts::TAU;
    let left = |h: P| [-h[1], h[0]];
    let (a0, a1) = (h0[1].datan2(h0[0]), h1[1].datan2(h1[0]));
    let mut best: Option<Plan> = None;
    for &s0 in &[1.0f64, -1.0] {
        for &s1 in &[1.0f64, -1.0] {
            let c0 = add(p0, mul(left(h0), s0 * radius));
            let c1 = add(p1, mul(left(h1), s1 * radius));
            let v = sub(c1, c0);
            let dist = hyp(v);
            let (a, straight);
            if s0 == s1 {
                if dist < 1e-9 { a = a0; straight = 0.0; } else { a = v[1].datan2(v[0]); straight = dist; }
            } else {
                if dist < 2.0 * radius + 1e-9 { continue; }
                straight = (dist * dist - 4.0 * radius * radius).sqrt();
                a = v[1].datan2(v[0]) + s0 * (2.0 * radius).datan2(straight);
            }
            let mut t0 = pymod(s0 * (a - a0), tau);
            if t0 > tau - 1e-6 { t0 = 0.0; }
            let mut t1 = pymod(s1 * (a1 - a), tau);
            if t1 > tau - 1e-6 { t1 = 0.0; }
            if t0 + t1 > prm.dubins_turn_max { continue; }
            let length = radius * (t0 + t1) + straight;
            if best.map_or(true, |b| length < b.length - 1e-9) {
                best = Some(Plan { length, p0, c0, s0, t0, c1, s1, t1, p1, a, straight });
            }
        }
    }
    best
}

fn dubins_pts(pl: &Plan, prm: &Params) -> Vec<P> {
    let d = [pl.a.dcos(), pl.a.dsin()];
    let first = if pl.t0 > 1e-9 { arc_pts(pl.c0, pl.p0, pl.s0, pl.t0, prm) } else { vec![pl.p0] };
    let mid = first[first.len() - 1];
    let end = add(mid, mul(d, pl.straight));
    let second = if pl.t1 > 1e-9 { arc_pts(pl.c1, end, pl.s1, pl.t1, prm) } else { vec![end] };
    let mut pts: Vec<P> = first;
    pts.extend(second);
    let n = pts.len();
    pts[0] = pl.p0;
    pts[n - 1] = pl.p1;
    let mut out = vec![pts[0]];
    for i in 1..n {
        if hyp(sub(pts[i], pts[i - 1])) > 1e-6 { out.push(pts[i]); }
    }
    out
}

// -------------------------------------------------------------------------------------------------------- lanes

struct Net {
    lanes: Vec<(Vec<P>, i64)>,
    cum: Vec<Vec<f64>>,
    a: Vec<P>,
    b: Vec<P>,
    li: Vec<usize>,
    st: Vec<f64>,
}

struct Hit { s: f64, lane: usize, sx: f64, t: P }

impl Net {
    fn new(lanes: Vec<(Vec<P>, i64)>) -> Net {
        let lanes: Vec<(Vec<P>, i64)> = lanes.into_iter().filter(|(xy, _)| xy.len() >= 2).collect();
        let (mut a, mut b, mut li, mut st, mut cums) = (vec![], vec![], vec![], vec![], vec![]);
        for (k, (xy, _)) in lanes.iter().enumerate() {
            let c = cumulative(xy);
            for j in 0..xy.len() - 1 {
                a.push(xy[j]);
                b.push(xy[j + 1]);
                li.push(k);
                st.push(c[j]);
            }
            cums.push(c);
        }
        Net { lanes, cum: cums, a, b, li, st }
    }

    fn hits(&self, p: P, d: P, lo: f64, hi: f64, prm: &Params) -> Vec<Hit> {
        let mut out = vec![];
        for j in 0..self.a.len() {
            let e = sub(self.b[j], self.a[j]);
            let den = d[0] * e[1] - d[1] * e[0];
            let w = sub(self.a[j], p);
            let ok = den.abs() > 1e-12;
            let sden = if ok { den } else { 1.0 };
            let s = (w[0] * e[1] - w[1] * e[0]) / sden;
            let f = (w[0] * d[1] - w[1] * d[0]) / sden;
            let ln = e[0].dhypot(e[1]);
            let sin = den.abs() / fmax(ln, 1e-12);
            if ok && f >= 0.0 && f <= 1.0 && s >= lo && s <= hi && sin >= prm.min_sin {
                out.push(Hit { s, lane: self.li[j], sx: self.st[j] + f * ln, t: div(e, ln) });
            }
        }
        out.sort_by(|x, y| round6(x.s).partial_cmp(&round6(y.s)).unwrap_or(std::cmp::Ordering::Equal)
            .then(x.lane.cmp(&y.lane)));
        out
    }

    fn at(&self, k: usize, station: f64, prm: &Params) -> Option<(P, P)> {
        let xy = &self.lanes[k].0;
        let cum = &self.cum[k];
        let l = cum[cum.len() - 1];
        if -1e-6 <= station && station <= l + 1e-6 {
            let s = fmin(fmax(station, 0.0), l);
            let j = (search_right(cum, s) as i64 - 1).max(0).min(xy.len() as i64 - 2) as usize;
            let seg = sub(xy[j + 1], xy[j]);
            let sl = fmax(hyp(seg), 1e-12);
            return Some((add(xy[j], mul(seg, (s - cum[j]) / sl)), div(seg, sl)));
        }
        let n = xy.len();
        let (end, od, over) = if station > l { (xy[n - 1], sub(xy[n - 1], xy[n - 2]), station - l) }
                              else { (xy[0], sub(xy[0], xy[1]), -station) };
        let out_dir = div(od, fmax(hyp(od), 1e-12));
        for (k2, (xy2, _)) in self.lanes.iter().enumerate() {
            if k2 == k { continue; }
            let m = xy2.len();
            for &(p, q, sign) in &[(xy2[0], xy2[1], 1.0f64), (xy2[m - 1], xy2[m - 2], -1.0)] {
                if hyp(sub(p, end)) > prm.chain_m { continue; }
                let dirn = div(sub(q, p), fmax(hyp(sub(q, p)), 1e-12));
                if dirn[0] * out_dir[0] + dirn[1] * out_dir[1] < prm.chain_cos { continue; }
                let l2 = self.cum[k2][self.cum[k2].len() - 1];
                if over > l2 { return None; }
                let (pt, t) = self.at(k2, if sign > 0.0 { over } else { l2 - over }, prm)?;
                let away = if sign > 0.0 { t } else { neg(t) };
                return Some((pt, if station > l { away } else { neg(away) }));
            }
        }
        None
    }

    fn samples(&self, step: f64, prm: &Params) -> Vec<(P, P, i64)> {
        let mut out = vec![];
        for (k, (_xy, wi)) in self.lanes.iter().enumerate() {
            let l = self.cum[k][self.cum[k].len() - 1];
            let n = fmax(1.0, (l / step).ceil()) as i64;
            for i in 0..=n {
                if let Some((p, t)) = self.at(k, l * (i as f64) / (n as f64), prm) {
                    out.push((p, t, *wi));
                }
            }
        }
        out
    }

    /// standpaths._closest: every segment's nearest point to p and its unit tangent.
    fn closest(&self, p: P) -> Vec<(P, P)> {
        (0..self.a.len()).map(|j| {
            let e = sub(self.b[j], self.a[j]);
            let ln = fmax(e[0].dhypot(e[1]), 1e-12);
            let t = div(e, ln);
            let w = sub(p, self.a[j]);
            let s = clip(w[0] * t[0] + w[1] * t[1], 0.0, ln);
            (add(self.a[j], mul(t, s)), t)
        }).collect()
    }
}

// ---------------------------------------------------------------------------------------------------- the parts

fn curve(s: P, hs: P, t: P, ht: P, radius: f64, rmin: f64, ok: &Clear, own: i64, prm: &Params) -> Option<Vec<P>> {
    for &k in &prm.radius_steps {
        let r = fmax(rmin, radius * k);
        if let Some(plan) = dubins(s, hs, t, ht, r, prm) {
            let pts = dubins_pts(&plan, prm);
            if ok.ok(&pts, own, prm) { return Some(pts); }
        }
        if r <= rmin { break; }
    }
    None
}

fn connect(net: &Net, p: P, h: P, rmin: f64, radius: f64, ok: &Clear, arrive: bool, prm: &Params) -> Option<Vec<P>> {
    let mut cand: Vec<(f64, P, P, i64)> = net.samples(prm.conn_step_m, prm).into_iter()
        .filter_map(|(q, t, wi)| { let d = hyp(sub(q, p)); if d <= prm.conn_max_m { Some((d, q, t, wi)) } else { None } })
        .collect();
    cand.sort_by(|x, y| round6(x.0).partial_cmp(&round6(y.0)).unwrap_or(std::cmp::Ordering::Equal).then(x.3.cmp(&y.3)));
    let mut best: Option<(f64, Vec<P>)> = None;
    for &(_d, q, t, wi) in cand.iter().take(prm.conn_tries) {
        for &sgn in &[1.0f64, -1.0] {
            let tt = mul(t, sgn);
            for &r in &[radius, rmin] {
                let plan = if arrive { dubins(q, tt, p, h, r, prm) } else { dubins(p, h, q, tt, r, prm) };
                let plan = match plan { Some(pl) => pl, None => continue };
                if plan.length > prm.conn_max_m || best.as_ref().map_or(false, |b| plan.length >= b.0) { continue; }
                let pts = dubins_pts(&plan, prm);
                if ok.ok(&pts, wi, prm) {
                    best = Some((plan.length, pts));
                    break;
                }
            }
        }
    }
    best.map(|b| b.1)
}

fn free_ends(net: &Net, aprons: &[Vec<P>], prm: &Params) -> Vec<(P, P, i64)> {
    let mut edges: Vec<(P, P)> = vec![];
    for r in aprons {
        if r.len() < 3 { continue; }
        for i in 0..r.len() { edges.push((r[i], r[(i + 1) % r.len()])); }
    }
    let mut out = vec![];
    for (k, (xy, wi)) in net.lanes.iter().enumerate() {
        if *wi < 0 { continue; }
        let segs: Vec<(P, P)> = (0..net.a.len()).filter(|&j| net.li[j] != k).map(|j| (net.a[j], net.b[j])).collect();
        let n = xy.len();
        for &(end, nxt) in &[(xy[0], xy[1]), (xy[n - 1], xy[n - 2])] {
            if !segs.is_empty() && pt_dist(end, &segs) <= prm.free_on_m { continue; }
            if !edges.is_empty() && pt_dist(end, &edges) <= prm.free_edge_m { continue; }
            let d = sub(end, nxt);
            out.push((end, div(d, fmax(hyp(d), 1e-12)), *wi));
        }
    }
    out
}

fn from_end(p: P, h: P, ends: &[(P, P, i64)], rmin: f64, radius: f64, ok: &Clear,
            used: &std::collections::BTreeSet<usize>, prm: &Params) -> Option<(Vec<P>, usize)> {
    let mut best: Option<(f64, Vec<P>, usize)> = None;
    for (j, &(e, d, wi)) in ends.iter().enumerate() {
        if used.contains(&j) || hyp(sub(e, p)) > prm.conn_max_m { continue; }
        for &r in &[radius, rmin] {
            let plan = match dubins(p, h, e, neg(d), r, prm) { Some(pl) => pl, None => continue };
            if plan.length > prm.conn_max_m || best.as_ref().map_or(false, |b| plan.length >= b.0) { continue; }
            let pts = dubins_pts(&plan, prm);
            if ok.ok(&pts, wi, prm) {
                best = Some((plan.length, pts, j));
                break;
            }
        }
    }
    best.map(|b| (b.1, b.2))
}

/// standpaths._groups over items[idx[..]] (indices into idx returned).
fn groups(items: &[Item], idx: &[usize], prm: &Params) -> Vec<Vec<usize>> {
    let n = idx.len();
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut Vec<usize>, mut i: usize) -> usize {
        while parent[i] != i {
            parent[i] = parent[parent[i]];
            i = parent[i];
        }
        i
    }
    for i in 0..n {
        for j in i + 1..n {
            let (a, b) = (&items[idx[i]], &items[idx[j]]);
            if hyp(sub(a.nose, b.nose)) <= (a.span + b.span) / 2.0 + prm.spine_link_m
                && a.u[0] * b.u[0] + a.u[1] * b.u[1] >= prm.spine_link_cos {
                let (ri, rj) = (find(&mut parent, i), find(&mut parent, j));
                let (lo, hi) = (ri.min(rj), ri.max(rj));
                parent[hi] = lo;
            }
        }
    }
    let mut rows: std::collections::BTreeMap<usize, Vec<usize>> = std::collections::BTreeMap::new();
    for i in 0..n {
        let r = find(&mut parent, i);
        rows.entry(r).or_default().push(i);
    }
    rows.into_values().collect()
}

/// The mean of the members' unit directions (numpy's mean over axis 0: the rows summed in order, over their count).
fn mean_u(items: &[Item], members: &[usize]) -> P {
    let mut s = [0.0, 0.0];
    for &i in members { s = add(s, items[i].u); }
    div(s, members.len() as f64)
}

fn fan_centre(items: &[Item], members: &[usize]) -> P {
    let (mut a11, mut a12, mut a22, mut b1, mut b2) = (0.0, 0.0, 0.0, 0.0, 0.0);
    for &i in members {
        let ([nx, ny], [ux, uy]) = (items[i].nose, items[i].u);
        let (m11, m12, m22) = (1.0 - ux * ux, -ux * uy, 1.0 - uy * uy);
        a11 += m11; a12 += m12; a22 += m22;
        b1 = b1 + m11 * nx + m12 * ny;          // (Python's b1 + m11 * nx + m12 * ny: left to right)
        b2 = b2 + m12 * nx + m22 * ny;
    }
    let det = a11 * a22 - a12 * a12;
    if det.abs() < 1e-6 {
        let mut s = [0.0, 0.0];
        for &i in members { s = add(s, items[i].nose); }
        div(s, members.len() as f64)
    } else {
        [(a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det]
    }
}

fn fan_order(ang: &[f64], members: &[usize]) -> Vec<usize> {
    let mut order: Vec<usize> = (0..ang.len()).collect();
    order.sort_by(|&x, &y| round6(ang[x]).partial_cmp(&round6(ang[y])).unwrap_or(std::cmp::Ordering::Equal)
        .then(members[x].cmp(&members[y])));
    let a: Vec<f64> = order.iter().map(|&k| ang[k]).collect();
    let n = a.len();
    let mut gaps: Vec<f64> = (0..n - 1).map(|k| a[k + 1] - a[k]).collect();
    gaps.push(a[0] + std::f64::consts::TAU - a[n - 1]);
    let mut g = 0;
    for k in 1..gaps.len() {
        if gaps[k] > gaps[g] { g = k; }          // (max by (gap, -k): the first of equal gaps)
    }
    let mut out: Vec<usize> = order[g + 1..].to_vec();
    out.extend_from_slice(&order[..g + 1]);
    out
}

fn spine(items: &[Item], members: &[usize], rmin: f64, blocks: &[Block], paved: &[Vec<P>], prm: &Params) -> Vec<Vec<P>> {
    let back = members.iter().map(|&i| items[i].span).fold(f64::NEG_INFINITY, fmax) / 2.0 + prm.spine_clear_m;
    let mut tails: std::collections::BTreeMap<usize, P> = std::collections::BTreeMap::new();
    for &i in members {
        for &k in &prm.spine_back_steps {
            let t = sub(items[i].nose, mul(items[i].u, items[i].length + back * k));
            if !off_paving(&[t, t], paved, prm) {
                tails.insert(i, t);
                break;
            }
        }
    }
    let members: Vec<usize> = members.iter().copied().filter(|i| tails.contains_key(i)).collect();
    if members.is_empty() { return vec![]; }
    let mean = mean_u(items, &members);
    let ml = hyp(mean);
    let fan = ml < 1e-6 || members.iter().map(|&i| (items[i].u[0] * mean[0] + items[i].u[1] * mean[1]) / ml)
        .fold(f64::INFINITY, fmin) < prm.fan_cos;
    let order: Vec<usize> = if fan {
        let c = fan_centre(items, &members);
        let ang: Vec<f64> = members.iter().map(|i| { let t = tails[i]; (t[1] - c[1]).datan2(t[0] - c[0]) }).collect();
        fan_order(&ang, &members).into_iter().map(|k| members[k]).collect()
    } else {
        let across = div([-mean[1], mean[0]], ml);
        let mut o = members.clone();
        o.sort_by(|&x, &y| {
            let (tx, ty) = (tails[&x], tails[&y]);
            round6(tx[0] * across[0] + tx[1] * across[1]).partial_cmp(&round6(ty[0] * across[0] + ty[1] * across[1]))
                .unwrap_or(std::cmp::Ordering::Equal).then(x.cmp(&y))
        });
        o
    };
    let pts: Vec<P> = order.iter().map(|i| tails[i]).collect();
    let (mut pieces, mut cur, mut who): (Vec<Vec<P>>, Vec<P>, Vec<Vec<usize>>) = (vec![], vec![pts[0]], vec![vec![order[0]]]);
    for j in 1..pts.len() {
        if through(&[pts[j - 1], pts[j]], blocks) {
            pieces.push(std::mem::take(&mut cur));
            who.push(vec![]);
        }
        cur.push(pts[j]);
        who.last_mut().unwrap().push(order[j]);
    }
    pieces.push(cur);
    let radius = fmax(rmin, prm.lens_r_k * members.iter().map(|&i| items[i].span).fold(f64::NEG_INFINITY, fmax));
    let mut out = vec![];
    for (mut q, w) in pieces.into_iter().zip(who.into_iter()) {
        if q.len() < 2 {
            let u = items[w[0]].u;
            let half = mul([-u[1], u[0]], items[w[0]].span / 2.0);
            q = vec![sub(q[0], half), add(q[0], half)];
        }
        let n = q.len();
        for &(end, nxt) in &[(0usize, 1usize), (n - 1, n - 2)] {
            let v = sub(q[end], q[nxt]);
            q[end] = add(q[end], div(v, fmax(hyp(v), 1e-12)));
        }
        let r = round_arcs(&q, radius, prm);
        if !through(&r, blocks) && !off_paving(&r, paved, prm) { out.push(r); }
    }
    out
}

fn lens(net: &Net, it: &Item, rmin: f64, ok: &Clear, nb: &[Option<f64>; 2], hit: &Hit, prm: &Params) -> Option<Vec<Vec<P>>> {
    let (nose, u) = (it.nose, it.u);
    let (s, k, sx, t) = (hit.s, hit.lane, hit.sx, hit.t);
    let x = sub(nose, mul(u, s));
    let room = s - it.straight;
    let r0 = fmax(rmin, prm.lens_r_k * it.span);
    let wi = net.lanes[k].1;
    let (mut lines, mut tips): (Vec<Vec<P>>, Vec<f64>) = (vec![], vec![]);
    let mut slit = 0usize;
    for &b in &prm.slit_back_m {
        let target = sub(x, mul(u, b));
        let any = net.closest(target).iter().any(|&(q, e)| {
            hyp(sub(q, target)) <= prm.slit_near_m && (e[0] * u[0] + e[1] * u[1]).abs() >= prm.slit_cos
        });
        if any { slit += 1; }
    }
    let slit = slit + 1 >= prm.slit_back_m.len();
    for &sign in &[1.0f64, -1.0] {
        let d = mul(t, sign);
        let turn = (d[0] * u[1] - d[1] * u[0]).abs().datan2(d[0] * u[0] + d[1] * u[1]);
        if turn > prm.turn_max_rad || turn < prm.slit_turn_rad { continue; }
        let tan = (turn / 2.0).dtan();
        let lo = rmin * tan;
        if lo > room { continue; }
        let mut tl = fmin(fmin(fmax(r0 * tan, prm.lens_room_k * room), prm.lens_r_max_k * r0 * tan), room);
        let gap = if sign > 0.0 { nb[0] } else { nb[1] };    // (neighbours.get(-sign): nb[0] is side -1, nb[1] side +1)
        if let Some(g) = gap { tl = fmin(tl, fmax(lo, g / 2.0)); }
        if net.at(k, sx - sign * tl, prm).is_none() { continue; }
        for &(ks, kt) in &prm.lens_tries {
            let back = net.at(k, sx - sign * tl * ks, prm);
            if back.is_none() || tl * kt > room { continue; }
            let (sp, hs) = back.unwrap();
            if let Some(pts) = curve(sp, mul(hs, sign), add(x, mul(mul(u, tl), kt)), u, tl / tan, rmin, ok, wi, prm) {
                lines.push(pts);
                tips.push(tl * kt);
                break;
            }
        }
    }
    if slit { tips.push(0.0); }
    if lines.is_empty() && !slit { return None; }
    let far = if slit { s } else { s - tips.iter().copied().fold(f64::INFINITY, fmin) };
    lines.push(vec![sub(nose, mul(u, far)), nose]);
    Some(lines)
}

// ---------------------------------------------------------------------------------------------------------- build

pub fn build(items: &[Item], lanes: Vec<(Vec<P>, i64)>, pads: &Pads, blocks: &[Block], paved: &[Vec<P>],
             aprons: &[Vec<P>], prm: &Params) -> Out {
    let n = items.len();
    let all: Vec<usize> = (0..n).collect();
    let rows: Vec<(Vec<usize>, bool)> = groups(items, &all, prm).into_iter().map(|m| {
        let mean = mean_u(items, &m);
        let ring = m.len() >= prm.row_ring_min && hyp(mean) < prm.row_ring_mean;
        (m, ring)
    }).collect();
    let mut paint: Vec<(Vec<P>, u8, f64)> = vec![];
    let mut net = Net::new(lanes);
    let quads: Vec<(P, P, f64, f64)> = items.iter().map(|it| (it.nose, it.u, it.length, it.span)).collect();
    let reach = |net: &Net, i: usize| {
        let it = &items[i];
        net.hits(it.nose, neg(it.u), it.length, it.length + prm.reach_spans * it.span, prm)
    };

    // stage 1: a spine for each row (of two or more) none of whose stands reaches a lane, joined to the network
    let mut own: Vec<(Vec<P>, i64)> = vec![];
    let mut spines: Vec<(usize, bool)> = vec![];
    let ends = free_ends(&net, aprons, prm);
    let mut used = std::collections::BTreeSet::new();
    let ok_all = Clear { pads, others: quads.clone(), blocks, paved };
    for (members, _ring) in &rows {
        let lost: Vec<usize> = members.iter().copied().filter(|&i| reach(&net, i).is_empty()).collect();
        if lost.len() < 2 { continue; }
        for sub_ in groups(items, &lost, prm) {
            let sub_: Vec<usize> = sub_.into_iter().map(|j| lost[j]).collect();
            if sub_.len() < 2 { continue; }
            let rmin = sub_.iter().map(|&i| items[i].rmin).fold(f64::NEG_INFINITY, fmax);
            let radius = fmax(rmin, prm.lens_r_k * sub_.iter().map(|&i| items[i].span).fold(f64::NEG_INFINITY, fmax));
            for sp in spine(items, &sub_, rmin, blocks, paved, prm) {
                let rev: Vec<P> = sp.iter().rev().copied().collect();
                let mut end: Option<(Vec<P>, Vec<P>, usize)> = None;
                let mut best: Option<(Vec<P>, Vec<P>)> = None;
                for pp in [&sp, &rev] {
                    let m = pp.len();
                    let h = sub(pp[m - 1], pp[m - 2]);
                    let h = div(h, fmax(hyp(h), 1e-12));
                    if let Some((c, j)) = from_end(pp[m - 1], h, &ends, rmin, radius, &ok_all, &used, prm) {
                        if end.as_ref().map_or(true, |e| length(&c) < length(&e.1)) { end = Some((pp.clone(), c, j)); }
                    }
                }
                for pp in [&sp, &rev] {
                    let m = pp.len();
                    let h = sub(pp[m - 1], pp[m - 2]);
                    let h = div(h, fmax(hyp(h), 1e-12));
                    if let Some(c) = connect(&net, pp[m - 1], h, rmin, radius, &ok_all, false, prm) {
                        if best.as_ref().map_or(true, |b| length(&c) < length(&b.1)) { best = Some((pp.clone(), c)); }
                    }
                }
                if let Some((ep, ec, ej)) = end {
                    if best.as_ref().map_or(true, |b| length(&ec) <= prm.free_prefer_k * length(&b.1) + prm.free_prefer_m) {
                        best = Some((ep, ec));
                        used.insert(ej);
                    }
                }
                let line: Vec<P> = match &best {
                    None => sp.clone(),
                    Some((bp, bc)) => { let mut l = bp.clone(); l.extend_from_slice(&bc[1..]); l }
                };
                own.push((line.clone(), -2 - own.len() as i64));
                spines.push((paint.len(), best.is_some()));
                paint.push((line, 0, rmin));
            }
        }
    }
    if !own.is_empty() {
        let mut lanes2 = net.lanes.clone();
        lanes2.extend(own.iter().cloned());
        net = Net::new(lanes2);
    }

    // stage 2: each stand's lens off the first lane its reversed axis meets
    let hits: Vec<Vec<Hit>> = (0..n).map(|i| reach(&net, i)).collect();
    let mut on: std::collections::HashMap<usize, Vec<(f64, usize)>> = std::collections::HashMap::new();
    for (i, h) in hits.iter().enumerate() {
        if let Some(h0) = h.first() { on.entry(h0.lane).or_default().push((h0.sx, i)); }
    }
    let mut found: Vec<Option<[P; 2]>> = vec![None; n];
    for i in 0..n {
        let it = &items[i];
        let others: Vec<(P, P, f64, f64)> = (0..n).filter(|&j| j != i).map(|j| quads[j]).collect();
        let oks = [Clear { pads, others: others.clone(), blocks, paved }, Clear { pads, others, blocks, paved: &[] }];
        let mut got: Option<Vec<Vec<P>>> = None;
        for ok in &oks {
            for (nn, hit) in hits[i].iter().take(3).enumerate() {
                let mut nb: [Option<f64>; 2] = [None, None];
                if nn == 0 {
                    if let Some(list) = on.get(&hit.lane) {
                        for &(st, j) in list {
                            if j == i { continue; }
                            let g = st - hit.sx;
                            let side = if g > 0.0 { 1 } else { 0 };
                            if g.abs() > 1e-6 && nb[side].map_or(true, |v| g.abs() < v) { nb[side] = Some(g.abs()); }
                        }
                    }
                }
                got = lens(&net, it, it.rmin, ok, &nb, hit, prm);
                if got.is_some() { break; }
            }
            if got.is_none() {
                let st = sub(it.nose, mul(it.u, it.straight));
                got = connect(&net, st, it.u, it.rmin, fmax(it.rmin, prm.lens_r_k * it.span), ok, true, prm)
                    .map(|c| vec![c, vec![st, it.nose]]);
            }
            if got.is_some() { break; }
        }
        let l = got.unwrap_or_else(|| vec![vec![sub(it.nose, mul(it.u, it.length + prm.lead_stub_m)), it.nose]]);
        let last = l.len() - 1;
        for xy in &l[..last] { paint.push((xy.clone(), 1, it.rmin)); }
        found[i] = Some([l[last][0], l[last][l[last].len() - 1]]);
    }

    // the spines' free ends cut back to the first lens (or slit) leaving them
    let mut starts: Vec<P> = paint.iter().filter(|x| x.1 == 1).map(|x| x.0[0]).collect();
    starts.extend(found.iter().flatten().map(|f| f[0]));
    for (&(k, joined), (line, _wi)) in spines.iter().zip(own.iter()) {
        let st: Vec<f64> = starts.iter().filter(|&&p| project_dist(line, p) < prm.trim_tol_m).map(|&p| station(line, p)).collect();
        if st.is_empty() {
            paint[k].0 = vec![];
            continue;
        }
        let lo = st.iter().copied().fold(f64::INFINITY, fmin);
        let hi = if joined { length(line) } else { st.iter().copied().fold(f64::NEG_INFINITY, fmax) };
        paint[k].0 = if hi - lo > 0.3 { subpath(line, lo, hi) } else { vec![] };
    }
    let mut paint: Vec<(Vec<P>, u8, f64)> = paint.into_iter().filter(|x| x.0.len() >= 2).collect();

    // a mapped lane's end still left open on the apron: joined to the nearest spine or lead-in
    let lines: Vec<(Vec<P>, i64)> = paint.iter().enumerate().map(|(j, x)| (x.0.clone(), -100 - j as i64)).collect();
    if !lines.is_empty() {
        let segs: Vec<(P, P)> = lines.iter().flat_map(|(xy, _)| (0..xy.len() - 1).map(move |i| (xy[i], xy[i + 1]))).collect();
        let near = Net::new(lines);
        for (j, &(e, d, _wi)) in ends.iter().enumerate() {
            if used.contains(&j) || pt_dist(e, &segs) <= prm.free_on_m { continue; }
            if let Some(c) = connect(&near, e, d, prm.min_radius_m, prm.lens_r_k * 36.0, &ok_all, false, prm) {
                if length(&c) <= prm.free_reach_m { paint.push((c, 0, prm.min_radius_m)); }
            }
        }
    }
    Out { paint, found, rows }
}

// ------------------------------------------------------------------------------------------------------- binding

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

fn pts2(a: &numpy::ndarray::ArrayView2<f64>) -> Vec<P> { (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect() }

fn rings(pts: &[P], starts: &[i64]) -> PyResult<Vec<Vec<P>>> {
    let mut out = vec![];
    for w in starts.windows(2) {
        if w[0] < 0 || w[1] < w[0] || w[1] as usize > pts.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("sp_build: starts rising within the points"));
        }
        out.push(pts[w[0] as usize..w[1] as usize].to_vec());
    }
    Ok(out)
}

fn col(a: &PyReadonlyArray1<'_, f64>) -> Vec<f64> { a.as_array().iter().copied().collect() }
fn coli(a: &PyReadonlyArray1<'_, i64>) -> Vec<i64> { a.as_array().iter().copied().collect() }

type Built<'py> = (Bound<'py, numpy::PyArray2<f64>>, Vec<i64>, Vec<u8>, Vec<f64>, Bound<'py, numpy::PyArray2<f64>>,
                   Vec<bool>, Vec<i64>, Vec<i64>, Vec<bool>);

/// standpaths.build (the stands' lead-ins: the lanes, then each stand's lens off its lane): the stands (nose, u (n, 2),
/// length, span, final straight, smallest radius (n,)), the lanes (points (m, 2), starts (L + 1,), way indices (L,)), the
/// runways' pads (a, b (p, 2), half widths, way indices (p,)), the buildings (ring points, starts, boxes (B, 4)), the
/// paving's rings and the aprons' (points, starts), the constants (standpaths._params()). -> (paint points (N, 2), starts
/// (K + 1,), roles (K,): 0 spine, 1 lead, smallest radii (K,), the final straights (n, 4), found (n,), the rows' members
/// (M,), starts (R + 1,), ring (R,)).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn sp_build<'py>(py: Python<'py>, nose: PyReadonlyArray2<'py, f64>, u: PyReadonlyArray2<'py, f64>,
                 length: PyReadonlyArray1<'py, f64>, span: PyReadonlyArray1<'py, f64>,
                 straight: PyReadonlyArray1<'py, f64>, rmin: PyReadonlyArray1<'py, f64>,
                 lane_pts: PyReadonlyArray2<'py, f64>, lane_starts: PyReadonlyArray1<'py, i64>,
                 lane_wi: PyReadonlyArray1<'py, i64>, pad_a: PyReadonlyArray2<'py, f64>, pad_b: PyReadonlyArray2<'py, f64>,
                 pad_half: PyReadonlyArray1<'py, f64>, pad_way: PyReadonlyArray1<'py, i64>,
                 blk_pts: PyReadonlyArray2<'py, f64>, blk_starts: PyReadonlyArray1<'py, i64>,
                 blk_box: PyReadonlyArray2<'py, f64>, paved_pts: PyReadonlyArray2<'py, f64>,
                 paved_starts: PyReadonlyArray1<'py, i64>, apron_pts: PyReadonlyArray2<'py, f64>,
                 apron_starts: PyReadonlyArray1<'py, i64>, params: PyReadonlyArray1<'py, f64>,
                 tries: PyReadonlyArray1<'py, f64>, radius_steps: PyReadonlyArray1<'py, f64>,
                 slit_back: PyReadonlyArray1<'py, f64>, back_steps: PyReadonlyArray1<'py, f64>) -> PyResult<Built<'py>> {
    let bad = |what: &str| pyo3::exceptions::PyValueError::new_err(format!("sp_build: {what}"));
    let (nv, uv) = (nose.as_array(), u.as_array());
    let n = nv.nrows();
    let (lv, sv, tv, rv) = (length.as_array(), span.as_array(), straight.as_array(), rmin.as_array());
    if (n > 0 && (nv.ncols() < 2 || uv.ncols() < 2)) || uv.nrows() != n || lv.len() != n || sv.len() != n
        || tv.len() != n || rv.len() != n {
        return Err(bad("stands: nose, u (n, 2), length, span, straight, rmin (n,)"));
    }
    let items: Vec<Item> = (0..n).map(|i| Item { nose: [nv[[i, 0]], nv[[i, 1]]], u: [uv[[i, 0]], uv[[i, 1]]],
                                                  length: lv[i], span: sv[i], straight: tv[i], rmin: rv[i] }).collect();
    let lr = rings(&pts2(&lane_pts.as_array()), &coli(&lane_starts))?;
    let lw = coli(&lane_wi);
    if lr.len() != lw.len() { return Err(bad("lanes: way indices (L,) for starts (L + 1,)")); }
    let lanes: Vec<(Vec<P>, i64)> = lr.into_iter().zip(lw).collect();
    let (pa, pb) = (pts2(&pad_a.as_array()), pts2(&pad_b.as_array()));
    let (ph, pw) = (col(&pad_half), coli(&pad_way));
    if pb.len() != pa.len() || ph.len() != pa.len() || pw.len() != pa.len() {
        return Err(bad("pads: a, b (p, 2), half, way (p,)"));
    }
    let pads = Pads { a: pa, b: pb, half: ph, way: pw };
    let br = rings(&pts2(&blk_pts.as_array()), &coli(&blk_starts))?;
    let bb = blk_box.as_array();
    if bb.nrows() != br.len() || (bb.nrows() > 0 && bb.ncols() < 4) || br.iter().any(|r| r.is_empty()) {
        return Err(bad("blocks: rings, boxes (B, 4) for starts (B + 1,)"));
    }
    let blocks: Vec<Block> = br.into_iter().enumerate()
        .map(|(i, ring)| Block { ring, bx: [bb[[i, 0]], bb[[i, 1]], bb[[i, 2]], bb[[i, 3]]] }).collect();
    let paved = rings(&pts2(&paved_pts.as_array()), &coli(&paved_starts))?;
    let aprons = rings(&pts2(&apron_pts.as_array()), &coli(&apron_starts))?;
    if paved.iter().chain(aprons.iter()).any(|r| r.is_empty()) { return Err(bad("paved / aprons: an empty ring")); }
    let prm = Params::from(&col(&params), &col(&tries), &col(&radius_steps), &col(&slit_back), &col(&back_steps))
        .ok_or_else(|| bad("params: 35 constants, the tries in pairs"))?;
    let out = py.detach(|| build(&items, lanes, &pads, &blocks, &paved, &aprons, &prm));
    let mut flat: Vec<f64> = vec![];
    let (mut starts, mut roles, mut rmins) = (vec![0i64], vec![], vec![]);
    for (xy, role, r) in &out.paint {
        for p in xy { flat.push(p[0]); flat.push(p[1]); }
        starts.push((flat.len() / 2) as i64);
        roles.push(*role);
        rmins.push(*r);
    }
    let npts = flat.len() / 2;
    let mut fnd = vec![0.0f64; n * 4];
    let mut fok = vec![false; n];
    for (i, f) in out.found.iter().enumerate() {
        if let Some([a, b]) = f {
            fnd[i * 4..i * 4 + 4].copy_from_slice(&[a[0], a[1], b[0], b[1]]);
            fok[i] = true;
        }
    }
    let (mut mem, mut rst, mut ring) = (vec![], vec![0i64], vec![]);
    for (m, r) in &out.rows {
        mem.extend(m.iter().map(|&i| i as i64));
        rst.push(mem.len() as i64);
        ring.push(*r);
    }
    let pts = numpy::ndarray::Array2::from_shape_vec((npts, 2), flat).unwrap().into_pyarray(py);
    let found = numpy::ndarray::Array2::from_shape_vec((n, 4), fnd).unwrap().into_pyarray(py);
    Ok((pts, starts, roles, rmins, found, fok, mem, rst, ring))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sp_build, m)?)?;
    Ok(())
}
