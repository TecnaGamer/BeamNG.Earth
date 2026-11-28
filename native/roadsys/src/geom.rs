//! Basic deterministic geometry: + - * / and sqrt only (no FMA: Rust never contracts; no libm), integer-mm
//! predicates in i128.
pub type P2 = [f64; 2];
pub type I2 = [i64; 2];

#[inline] pub fn sub(a: P2, b: P2) -> P2 { [a[0] - b[0], a[1] - b[1]] }
#[inline] pub fn add(a: P2, b: P2) -> P2 { [a[0] + b[0], a[1] + b[1]] }
#[inline] pub fn scale(a: P2, k: f64) -> P2 { [a[0] * k, a[1] * k] }
#[inline] pub fn dot(a: P2, b: P2) -> f64 { a[0] * b[0] + a[1] * b[1] }
#[inline] pub fn cross(a: P2, b: P2) -> f64 { a[0] * b[1] - a[1] * b[0] }
#[inline] pub fn norm(a: P2) -> f64 { (a[0] * a[0] + a[1] * a[1]).sqrt() }
#[inline] pub fn unit(a: P2) -> Option<P2> {
    let n = norm(a);
    if n > 1e-9 { Some([a[0] / n, a[1] / n]) } else { None }
}
#[inline] pub fn lerp(a: P2, b: P2, u: f64) -> P2 { [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u] }
#[inline] pub fn mm(v: f64) -> i64 { (v * 1000.0).round() as i64 }
#[inline] pub fn mm2(p: P2) -> I2 { [mm(p[0]), mm(p[1])] }
#[inline] pub fn clampf(v: f64, lo: f64, hi: f64) -> f64 { if v < lo { lo } else if v > hi { hi } else { v } }
#[inline] pub fn absf(v: f64) -> f64 { if v < 0.0 { -v } else { v } }

/// The quintic smoothstep (v3 forks.rs's).
#[inline] pub fn quintic(u: f64) -> f64 {
    let u = clampf(u, 0.0, 1.0);
    u * u * u * (10.0 + u * (-15.0 + u * 6.0))
}

#[inline] pub fn orient(a: I2, b: I2, c: I2) -> i128 {
    let (bx, by) = ((b[0] - a[0]) as i128, (b[1] - a[1]) as i128);
    let (cx, cy) = ((c[0] - a[0]) as i128, (c[1] - a[1]) as i128);
    bx * cy - by * cx
}

#[inline] pub fn incircle(a: I2, b: I2, c: I2, d: I2) -> i128 {
    let r = |p: I2| [(p[0] - d[0]) as i128, (p[1] - d[1]) as i128];
    let (a, b, c) = (r(a), r(b), r(c));
    let l = |p: [i128; 2]| p[0] * p[0] + p[1] * p[1];
    let det = |p: [i128; 2], q: [i128; 2]| p[0] * q[1] - p[1] * q[0];
    l(a) * det(b, c) + l(b) * det(c, a) + l(c) * det(a, b)
}

/// A pseudo-angle in [0, 4) of direction d (monotone in the true angle, CCW from +x), arithmetic only.
pub fn pseudo_angle(d: P2) -> f64 {
    let s = absf(d[0]) + absf(d[1]);
    if s <= 0.0 { return 0.0; }
    let a = d[1] / s;
    if d[0] < 0.0 { 2.0 - a } else if a < 0.0 { 4.0 + a } else { a }
}

/// Segment intersection (proper or touching) of p0p1 and q0q1 in f64: (u, v) params.
pub fn seg_x(p0: P2, p1: P2, q0: P2, q1: P2) -> Option<(f64, f64)> {
    let r = sub(p1, p0);
    let s = sub(q1, q0);
    let den = cross(r, s);
    if absf(den) < 1e-12 { return None; }
    let qp = sub(q0, p0);
    let u = cross(qp, s) / den;
    let v = cross(qp, r) / den;
    if u >= 0.0 && u <= 1.0 && v >= 0.0 && v <= 1.0 { Some((u, v)) } else { None }
}

/// Integer segment intersection test (proper crossing or overlap) for ring simplicity checks.
pub fn iseg_cross(a: I2, b: I2, c: I2, d: I2) -> bool {
    let o1 = orient(a, b, c);
    let o2 = orient(a, b, d);
    let o3 = orient(c, d, a);
    let o4 = orient(c, d, b);
    if ((o1 > 0 && o2 < 0) || (o1 < 0 && o2 > 0)) && ((o3 > 0 && o4 < 0) || (o3 < 0 && o4 > 0)) { return true; }
    let on = |p: I2, q: I2, r: I2| {
        q[0] >= p[0].min(r[0]) && q[0] <= p[0].max(r[0]) && q[1] >= p[1].min(r[1]) && q[1] <= p[1].max(r[1])
    };
    (o1 == 0 && on(a, c, b)) || (o2 == 0 && on(a, d, b)) || (o3 == 0 && on(c, a, d)) || (o4 == 0 && on(c, b, d))
}

/// Area of the intersection of two triangles (m^2, f64; for the checks only).
pub fn tri_overlap(a: [P2; 3], b: [P2; 3]) -> f64 {
    let mut poly: Vec<P2> = a.to_vec();
    // b as CCW
    let bb = if cross(sub(b[1], b[0]), sub(b[2], b[0])) < 0.0 { [b[0], b[2], b[1]] } else { b };
    for k in 0..3 {
        let (e0, e1) = (bb[k], bb[(k + 1) % 3]);
        let e = sub(e1, e0);
        let inside = |p: P2| cross(e, sub(p, e0)) >= 0.0;
        let mut out = Vec::with_capacity(poly.len() + 2);
        for i in 0..poly.len() {
            let (p, q) = (poly[i], poly[(i + 1) % poly.len()]);
            let (ip, iq) = (inside(p), inside(q));
            if ip { out.push(p); }
            if ip != iq {
                let dp = cross(e, sub(p, e0));
                let dq = cross(e, sub(q, e0));
                let t = dp / (dp - dq);
                out.push(lerp(p, q, t));
            }
        }
        poly = out;
        if poly.len() < 3 { return 0.0; }
    }
    let mut ar = 0.0;
    for i in 0..poly.len() {
        let (p, q) = (poly[i], poly[(i + 1) % poly.len()]);
        ar += p[0] * q[1] - q[0] * p[1];
    }
    absf(ar) / 2.0
}

/// A polyline with arc lengths.
#[derive(Clone, Debug, Default)]
pub struct Line { pub p: Vec<P2>, pub s: Vec<f64> }

impl Line {
    pub fn new(p: Vec<P2>) -> Line {
        let mut s = Vec::with_capacity(p.len());
        let mut acc = 0.0;
        for i in 0..p.len() {
            if i > 0 { acc += norm(sub(p[i], p[i - 1])); }
            s.push(acc);
        }
        Line { p, s }
    }
    pub fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    /// The segment index k with s[k] <= t <= s[k+1].
    pub fn seg_of(&self, t: f64) -> usize {
        let n = self.p.len();
        if n < 2 { return 0; }
        // binary search
        let (mut lo, mut hi) = (0usize, n - 1);
        while hi - lo > 1 {
            let mid = (lo + hi) / 2;
            if self.s[mid] <= t { lo = mid; } else { hi = mid; }
        }
        lo.min(n - 2)
    }
    /// (point, unit tangent) at arc t (clamped).
    pub fn at(&self, t: f64) -> (P2, P2) {
        let n = self.p.len();
        if n < 2 { return (self.p[0], [1.0, 0.0]); }
        let t = clampf(t, 0.0, self.len());
        let k = self.seg_of(t);
        let l = self.s[k + 1] - self.s[k];
        let u = if l > 0.0 { (t - self.s[k]) / l } else { 0.0 };
        (lerp(self.p[k], self.p[k + 1], u), unit(sub(self.p[k + 1], self.p[k])).unwrap_or([1.0, 0.0]))
    }
    /// (point, unit left normal, mitre scale) at arc t: at an interior vertex the bisector normal and 1 / cos of the
    /// half turn (at most 2), else the segment's normal.
    pub fn frame(&self, t: f64) -> (P2, P2, f64) {
        let n = self.p.len();
        let (p, tg) = self.at(t);
        let nrm = [-tg[1], tg[0]];
        if n < 3 { return (p, nrm, 1.0); }
        let k = self.seg_of(t);
        // exactly at an interior vertex?
        let vi = if (self.s[k] - t) == 0.0 && k > 0 { Some(k) } else if (self.s[k + 1] - t) == 0.0 && k + 1 < n - 1 { Some(k + 1) } else { None };
        if let Some(v) = vi {
            let t0 = unit(sub(self.p[v], self.p[v - 1])).unwrap_or(tg);
            let t1 = unit(sub(self.p[v + 1], self.p[v])).unwrap_or(tg);
            let n0 = [-t0[1], t0[0]];
            let n1 = [-t1[1], t1[0]];
            if let Some(b) = unit(add(n0, n1)) {
                let c = dot(b, n0);
                let m = if c > 0.5 { 1.0 / c } else { 2.0 };
                return (self.p[v], b, m);
            }
        }
        (p, nrm, 1.0)
    }
    /// (arc, signed offset + left) of the nearest point to q over arcs [t0, t1]; ties: the first segment.
    pub fn project(&self, q: P2, t0: f64, t1: f64) -> (f64, f64) {
        let mut best = (f64::INFINITY, 0.0, 0.0);
        let n = self.p.len();
        for k in 0..n.saturating_sub(1) {
            if self.s[k + 1] < t0 || self.s[k] > t1 { continue; }
            let (a, b) = (self.p[k], self.p[k + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            let u = if l2 > 0.0 { clampf(dot(sub(q, a), ab) / l2, 0.0, 1.0) } else { 0.0 };
            let m = lerp(a, b, u);
            let d = norm(sub(q, m));
            if d < best.0 {
                let side = if cross(ab, sub(q, a)) < 0.0 { -1.0 } else { 1.0 };
                best = (d, self.s[k] + (self.s[k + 1] - self.s[k]) * u, side * d);
            }
        }
        (best.1, best.2)
    }
}

/// FNV-1a 64 over bytes (output identity, not security).
pub fn fnv(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes { h ^= b as u64; h = h.wrapping_mul(0x100000001b3); }
    h
}
