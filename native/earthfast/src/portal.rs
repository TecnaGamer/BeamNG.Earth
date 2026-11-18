//! Tunnel entrances (2026-11-12, the user: "focus on tunnel entrances ... Many entrances are broken or ugly and look
//! nothing like the real version"): the structure at a tube's mouth built from the ground round it, in place of the
//! one face 6 m wider and higher than the opening that stood at every portal (a concrete bunker on a hillside:
//! GitHub #403 Malibu Canyon, #84, #1066 "tunnel portals use the wrong models"; the real ones: a ring a hand wide
//! round the arch set in the rock at Kilbotn, a raked concrete hood on a motorway bore, a flat lintel between a
//! cutting's walls in a city).
//!
//! - `portal`: the collar (the tube's own outline carried out of the hill, its end raked back on a mountain bore),
//!   the headwall in the mouth's plane (its top a little over the ground beside and over the opening, no higher than
//!   the opening needs where the ground is lower: its shoulders slope down to the ground there), and the wing walls
//!   along the approach where its sides stand over the road (a cutting), each coming down with its ground.
//! - `cover`: the outside of a tube where the ground does not cover it (the terrain is holed there; the tube was
//!   faced inwards only: sky through its vault from outside, the scan's "thin" / "portal_open"): a box round it
//!   with soil on its top, as a cut-and-cover section's backfill.
//! - `style`: which of the looks a mouth gets, from the road and the ground round it.
//!
//! Local frame of a mouth: x to the left of the travel out of the tube, s out of the tube along its axis, h over the
//! floor at the mouth. Everything is one-sided, facing out of its solid. Pure.
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Material codes of the triangles (the glue maps them: roadmesh.tunnel).
pub const M_FACE: i64 = 0;      // the structure's concrete / stone
pub const M_SOIL: i64 = 1;      // ground on a cover's top
pub const M_COLLAR: i64 = 2;    // the collar round the opening (lighter: painted / fair-faced concrete, Loibl, Laerdal)

#[derive(Default)]
pub struct Mesh {
    pub p: Vec<[f64; 3]>,
    pub uv: Vec<[f64; 2]>,
    pub t: Vec<[i64; 3]>,
    pub m: Vec<i64>,
}

#[derive(Clone, Copy)]
enum Look {
    Dir([f64; 3]),
    Toward([f64; 3]),
    Away([f64; 3]),
}

fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] { [a[0] - b[0], a[1] - b[1], a[2] - b[2]] }
fn dot(a: [f64; 3], b: [f64; 3]) -> f64 { a[0] * b[0] + a[1] * b[1] + a[2] * b[2] }
fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

impl Mesh {
    /// Triangles over the vertices v (position, uv in m), each turned to face `look`; degenerate ones left out.
    fn add(&mut self, v: &[([f64; 3], [f64; 2])], tris: &[[usize; 3]], look: Look, mat: i64, uv_m: f64) {
        let base = self.p.len() as i64;
        for (p, uv) in v {
            self.p.push(*p);
            self.uv.push([uv[0] / uv_m, uv[1] / uv_m]);
        }
        for t in tris {
            let (a, b, c) = (v[t[0]].0, v[t[1]].0, v[t[2]].0);
            let n = cross(sub(b, a), sub(c, a));
            if dot(n, n) < 1e-14 { continue; }
            let cen = [(a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0, (a[2] + b[2] + c[2]) / 3.0];
            let want = match look {
                Look::Dir(d) => d,
                Look::Toward(q) => sub(q, cen),
                Look::Away(q) => sub(cen, q),
            };
            let (i, j, k) = (base + t[0] as i64, base + t[1] as i64, base + t[2] as i64);
            self.t.push(if dot(n, want) >= 0.0 { [i, j, k] } else { [i, k, j] });
            self.m.push(mat);
        }
    }

    fn quad(&mut self, q: [([f64; 3], [f64; 2]); 4], look: Look, mat: i64, uv_m: f64) {
        self.add(&q, &[[0, 1, 2], [0, 2, 3]], look, mat, uv_m);
    }
}

/// A mouth's frame: its floor's centre p, the unit vectors left (u) and out of the tube (o).
#[derive(Clone, Copy)]
pub struct Frame {
    pub p: [f64; 3],
    pub u: [f64; 2],
    pub o: [f64; 2],
}

impl Frame {
    fn w(&self, x: f64, s: f64, h: f64) -> [f64; 3] {
        [self.p[0] + self.u[0] * x + self.o[0] * s, self.p[1] + self.u[1] * x + self.o[1] * s, self.p[2] + h]
    }
    fn up(&self) -> [f64; 3] { [0.0, 0.0, 1.0] }
    fn out(&self) -> [f64; 3] { [self.o[0], self.o[1], 0.0] }
    fn left(&self) -> [f64; 3] { [self.u[0], self.u[1], 0.0] }
}

/// The looks of a mouth's structure.
#[derive(Clone, Copy, Debug)]
pub struct Prm {
    pub th: f64,            // the opening's height (floor to crown)
    pub hw: f64,            // its half width (centre to wall)
    pub collar_out: f64,    // m the collar stands out of the headwall at its crown ...
    pub rake: f64,          // ... and this much further at its feet (a raked hood)
    pub collar_t: f64,      // the collar's thickness (the ring seen from the road)
    pub wall_t: f64,        // the headwall's thickness (into the hill)
    pub parapet: f64,       // m the headwall's top stands over the ground behind it
    pub side: f64,          // m of headwall beside the opening on its left ...
    pub side_r: f64,        // ... and on its right (less where another road runs beside the mouth)
    pub flat: f64,          // m beside the opening the headwall keeps its height over the crown (the tube's cover
                            // behind it), before its shoulders slope down
    pub inner_from: f64,    // m out of the headwall the collar's inside begins (a hood over the tube's own end: the
                            // tube's walls are its inside up to the mouth; both drawn there flickered)
    pub top_min: f64,       // m of headwall over the crown at least ...
    pub top_max: f64,       // ... and at most (higher ground goes on as ground)
    pub foot: f64,          // m its foot reaches under the floor
    pub end_min: f64,       // m the headwall's ends stand over the floor at least (its shoulders slope to this)
    pub wing_t: f64,        // a wing wall's thickness
    pub wing_flare: f64,    // m a wing wall turns away from the road per m out
    pub wing_low: f64,      // m over the road where a wing wall ends
    pub uv_m: f64,          // m per repeat of its material
}

impl Prm {
    pub fn from_slice(v: &[f64]) -> Prm {
        Prm { th: v[0], hw: v[1], collar_out: v[2], rake: v[3], collar_t: v[4], wall_t: v[5], parapet: v[6],
              side: v[7], top_min: v[8], top_max: v[9], foot: v[10], end_min: v[11], wing_t: v[12],
              wing_flare: v[13], wing_low: v[14], uv_m: v[15],
              side_r: v.get(16).copied().unwrap_or(v[7]),
              flat: v.get(17).copied().unwrap_or(v[4]),
              inner_from: v.get(18).copied().unwrap_or(0.0) }
    }
}

fn interp(x: f64, xs: &[f64], ys: &[f64]) -> f64 {
    if xs.is_empty() { return f64::NAN; }
    if x <= xs[0] { return ys[0]; }
    for i in 1..xs.len() {
        if x <= xs[i] {
            let d = xs[i] - xs[i - 1];
            return if d <= 1e-12 { ys[i] } else { ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / d };
        }
    }
    ys[ys.len() - 1]
}

/// The headwall's top over the floor along x (the mouth's plane): `parapet` over the ground there (face_x ascending,
/// face_g the ground over the floor), over the opening and its collar at least th + top_min, sloping from there
/// down to end_min at the wall's ends where the ground is lower, nowhere over th + top_max. [(x, top)], ascending x.
pub fn headwall_top(prm: &Prm, face_x: &[f64], face_g: &[f64]) -> Vec<(f64, f64)> {
    let core = prm.hw + prm.collar_t.max(prm.flat);
    let (el, er) = (prm.hw + prm.side.max(prm.collar_t + 0.1), prm.hw + prm.side_r.max(prm.collar_t + 0.1));
    let mut xs: Vec<f64> = vec![-er, -core, 0.0, core, el];
    for &x in face_x {
        if x > -er + 0.2 && x < el - 0.2 && (x.abs() - core).abs() > 0.2 && x.abs() > 0.2 { xs.push(x); }
    }
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let cap = prm.th + prm.top_max;
    xs.iter().map(|&x| {
        let g = interp(x, face_x, face_g);
        let need = if x.abs() <= core + 1e-9 { prm.th + prm.top_min } else {
            let e = if x > 0.0 { el } else { er };
            let f = ((x.abs() - core) / (e - core).max(1e-6)).clamp(0.0, 1.0);
            (prm.th + prm.top_min) * (1.0 - f) + prm.end_min * f
        };
        let top = if g.is_finite() { (g + prm.parapet).max(need) } else { need };
        (x, top.min(cap).max(prm.end_min))
    }).collect()
}

/// The opening's outline from a tube's section (roadmesh._tunnel_section: the shoulders' floor points first and
/// last): its wall feet and vault, left to right, and their unit normals away from the opening.
fn outline(sec: &[[f64; 2]], th: f64) -> (Vec<[f64; 2]>, Vec<[f64; 2]>) {
    let o: Vec<[f64; 2]> = sec[1..sec.len() - 1].to_vec();
    let n = o.len();
    let mut nrm = Vec::with_capacity(n);
    for i in 0..n {
        let a = o[if i == 0 { 0 } else { i - 1 }];
        let b = o[if i + 1 == n { n - 1 } else { i + 1 }];
        let (tx, th_) = (b[0] - a[0], b[1] - a[1]);
        let l = tx.dhypot(th_).max(1e-9);
        let mut v = [th_ / l, -tx / l];
        // away from the opening's middle
        if v[0] * (o[i][0]) + v[1] * (o[i][1] - th / 2.0) < 0.0 { v = [-v[0], -v[1]]; }
        if i == 0 || i + 1 == n { v = [if o[i][0] >= 0.0 { 1.0 } else { -1.0 }, 0.0]; }   // (the feet: sideways)
        nrm.push(v);
    }
    (o, nrm)
}

/// The structure at a mouth (the module's head). sec: the tube's section (offset left, height) with its shoulders'
/// floor points first and last; face_x / face_g: the ground over the mouth's floor along its plane; wing_s: stations
/// out of the mouth, wing_l / wing_r: the ground over the mouth's floor behind each wing wall's line there, road_z:
/// the road's height over the mouth's floor at those stations (the approach's fall or climb); empty wing_s: no wings.
#[allow(clippy::too_many_arguments)]
pub fn portal(f: &Frame, prm: &Prm, sec: &[[f64; 2]], face_x: &[f64], face_g: &[f64], wing_s: &[f64],
              wing_l: &[f64], wing_r: &[f64], road_z: &[f64]) -> Mesh {
    let mut m = Mesh::default();
    if sec.len() < 5 { return m; }
    let (o, nrm) = outline(sec, prm.th);
    let n = o.len();
    let uv = prm.uv_m;
    let top = headwall_top(prm, face_x, face_g);
    let (el, er) = (top[top.len() - 1].0, -top[0].0);
    // the headwall: its front in the mouth's plane, its back wall_t into the hill
    let mut poly: Vec<(f64, f64)> = Vec::with_capacity(n + top.len() + 6);
    poly.push((o[0][0], -prm.foot));
    for q in &o { poly.push((q[0], q[1])); }
    poly.push((o[n - 1][0], -prm.foot));
    poly.push((-er, -prm.foot));
    for &(x, t) in &top { poly.push((x, t)); }
    poly.push((el, -prm.foot));
    // (ear_clip wants a counter-clockwise ring: this one runs clockwise seen with x to the right)
    let ring: Vec<(f64, f64)> = poly.iter().rev().cloned().collect();
    let tris: Vec<[usize; 3]> = crate::ear_clip(&ring).iter()
        .map(|t| [t[0] as usize, t[1] as usize, t[2] as usize]).collect();
    for (s, look) in [(0.0, f.out()), (-prm.wall_t, [-f.o[0], -f.o[1], 0.0])] {
        let v: Vec<([f64; 3], [f64; 2])> = ring.iter().map(|&(x, h)| (f.w(x, s, h), [x, h])).collect();
        m.add(&v, &tris, Look::Dir(look), M_FACE, uv);
    }
    for w in top.windows(2) {                           // its top
        let ((x0, t0), (x1, t1)) = (w[0], w[1]);
        m.quad([(f.w(x0, 0.0, t0), [x0, 0.0]), (f.w(x1, 0.0, t1), [x1, 0.0]),
                (f.w(x1, -prm.wall_t, t1), [x1, prm.wall_t]), (f.w(x0, -prm.wall_t, t0), [x0, prm.wall_t])],
               Look::Dir(f.up()), M_FACE, uv);
    }
    for (x, t, look) in [(-er, top[0].1, [-f.u[0], -f.u[1], 0.0]), (el, top[top.len() - 1].1, f.left())] {   // its ends
        m.quad([(f.w(x, 0.0, -prm.foot), [0.0, -prm.foot]), (f.w(x, 0.0, t), [0.0, t]),
                (f.w(x, -prm.wall_t, t), [prm.wall_t, t]), (f.w(x, -prm.wall_t, -prm.foot), [prm.wall_t, -prm.foot])],
               Look::Dir(look), M_FACE, uv);
    }
    // the collar: the opening's outline carried out of the wall, raked back from its feet to its crown
    if prm.collar_out + prm.rake > 0.02 && prm.collar_t > 0.02 {
        let st: Vec<f64> = o.iter().map(|q| prm.collar_out + prm.rake * (1.0 - (q[1] / prm.th).clamp(0.0, 1.0))).collect();
        let r: Vec<[f64; 2]> = (0..n).map(|i| [o[i][0] + nrm[i][0] * prm.collar_t, o[i][1] + nrm[i][1] * prm.collar_t]).collect();
        let mut arc = vec![0.0; n];
        for i in 1..n { arc[i] = arc[i - 1] + (o[i][0] - o[i - 1][0]).dhypot(o[i][1] - o[i - 1][1]); }
        for i in 0..n - 1 {
            let j = i + 1;
            let axis = f.w(0.0, (st[i] + st[j]) / 4.0, prm.th / 2.0);
            // inside (the tube's surface going on), the ring seen from the road, outside
            let (si, sj) = (prm.inner_from.min(st[i]), prm.inner_from.min(st[j]));
            m.quad([(f.w(o[i][0], si, o[i][1]), [arc[i], si]), (f.w(o[i][0], st[i], o[i][1]), [arc[i], st[i]]),
                    (f.w(o[j][0], st[j], o[j][1]), [arc[j], st[j]]), (f.w(o[j][0], sj, o[j][1]), [arc[j], sj])],
                   Look::Toward(axis), M_COLLAR, uv);
            m.quad([(f.w(o[i][0], st[i], o[i][1]), [o[i][0], o[i][1]]), (f.w(r[i][0], st[i], r[i][1]), [r[i][0], r[i][1]]),
                    (f.w(r[j][0], st[j], r[j][1]), [r[j][0], r[j][1]]), (f.w(o[j][0], st[j], o[j][1]), [o[j][0], o[j][1]])],
                   Look::Dir([f.o[0], f.o[1], 0.35]), M_COLLAR, uv);
            m.quad([(f.w(r[i][0], 0.0, r[i][1]), [arc[i], 0.0]), (f.w(r[i][0], st[i], r[i][1]), [arc[i], st[i]]),
                    (f.w(r[j][0], st[j], r[j][1]), [arc[j], st[j]]), (f.w(r[j][0], 0.0, r[j][1]), [arc[j], 0.0])],
                   Look::Away(axis), M_COLLAR, uv);
        }
    }
    // the wing walls: from the headwall's ends out along the approach, each top with the ground behind it, never
    // over the headwall's end, down to wing_low over the road where they end
    if wing_s.len() >= 2 {
        for (side, g, t0, e) in [(1.0, wing_l, top[top.len() - 1].1, el), (-1.0, wing_r, top[0].1, er)] {
            if g.len() != wing_s.len() { continue; }
            let mut pts: Vec<(f64, f64, f64, f64)> = Vec::new();    // (s, x, top, foot)
            pts.push((0.0, side * e, t0, -prm.foot));
            let mut cap = t0;
            for k in 0..wing_s.len() {
                let s = wing_s[k];
                if s <= 0.05 { continue; }
                let rz = road_z.get(k).copied().unwrap_or(0.0);
                let t = if g[k].is_finite() { (g[k] + prm.parapet * 0.5).min(cap) } else { f64::NEG_INFINITY };
                if t - rz < prm.wing_low {
                    // its end: down to wing_low where the top would cross it (between the last point and this)
                    let (ps, _px, pt, _pf) = pts[pts.len() - 1];
                    let prz = if k == 0 { 0.0 } else { road_z.get(k - 1).copied().unwrap_or(0.0) };
                    let (a, b) = (pt - prz - prm.wing_low, t - rz - prm.wing_low);
                    let fr = if a - b > 1e-9 && a > 0.0 && b.is_finite() { (a / (a - b)).clamp(0.0, 1.0) } else { 0.0 };
                    let se = ps + (s - ps) * fr;
                    if se - ps > 0.3 {
                        let rze = prz + (rz - prz) * fr;
                        pts.push((se, side * (e + prm.wing_flare * se), rze + prm.wing_low, rze - prm.foot));
                    }
                    break;
                }
                cap = t;
                pts.push((s, side * (e + prm.wing_flare * s), t, rz - prm.foot));
            }
            if pts.len() < 2 { continue; }
            let wt = prm.wing_t * side;
            let toward = [-side * f.u[0], -side * f.u[1], 0.0];        // to the road
            let away = [side * f.u[0], side * f.u[1], 0.0];
            for w in pts.windows(2) {
                let ((s0, x0, t0, f0), (s1, x1, t1, f1)) = (w[0], w[1]);
                m.quad([(f.w(x0, s0, f0), [s0, f0]), (f.w(x0, s0, t0), [s0, t0]), (f.w(x1, s1, t1), [s1, t1]),
                        (f.w(x1, s1, f1), [s1, f1])], Look::Dir(toward), M_FACE, uv);
                m.quad([(f.w(x0 + wt, s0, f0), [s0, f0]), (f.w(x0 + wt, s0, t0), [s0, t0]),
                        (f.w(x1 + wt, s1, t1), [s1, t1]), (f.w(x1 + wt, s1, f1), [s1, f1])], Look::Dir(away), M_FACE, uv);
                m.quad([(f.w(x0, s0, t0), [s0, 0.0]), (f.w(x1, s1, t1), [s1, 0.0]), (f.w(x1 + wt, s1, t1), [s1, prm.wing_t]),
                        (f.w(x0 + wt, s0, t0), [s0, prm.wing_t])], Look::Dir(f.up()), M_FACE, uv);
            }
            let (s1, x1, t1, f1) = pts[pts.len() - 1];
            m.quad([(f.w(x1, s1, f1), [0.0, f1]), (f.w(x1, s1, t1), [0.0, t1]), (f.w(x1 + wt, s1, t1), [prm.wing_t, t1]),
                    (f.w(x1 + wt, s1, f1), [prm.wing_t, f1])], Look::Dir(f.out()), M_FACE, uv);
        }
    }
    m
}

/// The outside of a tube along the rows where the ground does not cover it. mid (k, 2) its centre line, left (k, 2)
/// unit vectors to its left, z (k,) the floor, ground (k,) the ground over the centre line (NaN: not known, covered);
/// hw: the box's half width, th: the tube's height, over: the box's top over the crown, need: the ground this far
/// over the crown covers the tube (no box), foot: the box's sides reach this far under the floor; caps: (start, end)
/// close the box where a run ends at the line's end (no headwall there). Runs of uncovered rows, a row either side.
#[allow(clippy::too_many_arguments)]
pub fn cover(mid: &[[f64; 2]], left: &[[f64; 2]], z: &[f64], ground: &[f64], hw: f64, th: f64, over: f64,
             need: f64, foot: f64, caps: (bool, bool), uv_m: f64, soil_uv_m: f64, opening: &[[f64; 2]]) -> Mesh {
    let mut m = Mesh::default();
    let k = mid.len();
    if k < 2 || left.len() != k || z.len() != k || ground.len() != k { return m; }
    let bare: Vec<bool> = (0..k).map(|i| ground[i].is_finite() && ground[i] - z[i] < th + need).collect();
    let mut open = vec![false; k];
    for i in 0..k {
        if bare[i] {
            open[i] = true;
            if i > 0 { open[i - 1] = true; }
            if i + 1 < k { open[i + 1] = true; }
        }
    }
    let mut s = vec![0.0; k];
    for i in 1..k { s[i] = s[i - 1] + (mid[i][0] - mid[i - 1][0]).dhypot(mid[i][1] - mid[i - 1][1]); }
    let pt = |i: usize, x: f64, h: f64| -> [f64; 3] { [mid[i][0] + left[i][0] * x, mid[i][1] + left[i][1] * x, z[i] + h] };
    let top = th + over;
    let mut i = 0;
    while i < k {
        if !open[i] { i += 1; continue; }
        let mut j = i;
        while j + 1 < k && open[j + 1] { j += 1; }
        if j > i {
            for a in i..j {
                let b = a + 1;
                let axis = [(mid[a][0] + mid[b][0]) / 2.0, (mid[a][1] + mid[b][1]) / 2.0, (z[a] + z[b]) / 2.0 + th / 2.0];
                for side in [1.0, -1.0] {
                    m.quad([(pt(a, side * hw, -foot), [s[a], -foot]), (pt(a, side * hw, top), [s[a], top]),
                            (pt(b, side * hw, top), [s[b], top]), (pt(b, side * hw, -foot), [s[b], -foot])],
                           Look::Away(axis), M_FACE, uv_m);
                }
                m.quad([(pt(a, hw, top), [hw, s[a]]), (pt(a, -hw, top), [-hw, s[a]]), (pt(b, -hw, top), [-hw, s[b]]),
                        (pt(b, hw, top), [hw, s[b]])], Look::Dir([0.0, 0.0, 1.0]), M_SOIL, soil_uv_m);
            }
            // its ends inside the hill: the box's section less the tube's opening (a full rectangle stood across the
            // tunnel's inside, a wall in the way, until 2026-11-12's review of the handed-over kernel)
            for (at, nb, cap) in [(i, i + 1, i > 0 || caps.0), (j, j - 1, j + 1 < k || caps.1)] {
                if !cap || opening.len() < 3 { continue; }
                let d = [mid[at][0] - mid[nb][0], mid[at][1] - mid[nb][1], 0.0];
                let n = opening.len();
                let mut poly: Vec<(f64, f64)> = Vec::with_capacity(n + 6);
                poly.push((opening[0][0], -foot));
                for q in opening { poly.push((q[0], q[1])); }
                poly.push((opening[n - 1][0], -foot));
                poly.push((-hw, -foot));
                poly.push((-hw, top));
                poly.push((hw, top));
                poly.push((hw, -foot));
                let ring: Vec<(f64, f64)> = poly.iter().rev().cloned().collect();
                let tris: Vec<[usize; 3]> = crate::ear_clip(&ring).iter()
                    .map(|t| [t[0] as usize, t[1] as usize, t[2] as usize]).collect();
                let v: Vec<([f64; 3], [f64; 2])> = ring.iter().map(|&(x, h)| (pt(at, x, h), [x, h])).collect();
                m.add(&v, &tris, Look::Dir(d), M_FACE, uv_m);
            }
        }
        i = j + 1;
    }
    m
}

/// A mouth's look from its road and ground: 0 a mountain bore (a raked hood, a headwall into the slope), 1 a
/// cut-and-cover mouth between a cutting's walls or under flat ground (a square-set collar, a level lintel, no rake),
/// 2 a minor road's mouth in a hillside (a shorter hood). major: a motorway .. primary; rise: the ground over the
/// floor ~12 m inside less the ground at the mouth (how fast the hill climbs over the tube); sides: the lower of the
/// approach's two sides over the road 5 .. 10 m out (a cutting's depth). A bore into a hill keeps its hood out of a
/// rock cut too (Kilbotn, Laerdal: the first rule, a cut of 0.45 th, made them flat lintels); only a trough deeper
/// than the opening (its walls over the mouth: an urban U-trough) gets the lintel.
pub fn style(major: bool, th: f64, rise: f64, sides: f64) -> i64 {
    let hill = rise >= 0.5 * th;
    if !hill || sides >= th + 2.0 { return 1; }
    if major { 0 } else { 2 }
}

fn mesh_out<'py>(py: Python<'py>, m: Mesh) -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>,
                                                 Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>) {
    let (np_, nt) = (m.p.len(), m.t.len());
    let p: Vec<f64> = m.p.iter().flat_map(|v| v.iter().cloned()).collect();
    let t: Vec<i64> = m.t.iter().flat_map(|v| v.iter().cloned()).collect();
    let uv: Vec<f64> = m.uv.iter().flat_map(|v| v.iter().cloned()).collect();
    (numpy::ndarray::Array2::from_shape_vec((np_, 3), p).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((nt, 3), t).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((np_, 2), uv).unwrap().into_pyarray(py),
     m.m.into_pyarray(py))
}

/// roadmesh.tunnel's portal structure: `portal` for the mouth at p (its floor's centre, the mesh's coordinates) with
/// unit vectors left / out; prm as Prm::from_slice. (positions (n, 3), triangles (m, 3), uvs (n, 2), material code
/// per triangle: 0 the structure, 1 soil.)
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn portal_mesh<'py>(py: Python<'py>, p: [f64; 3], left: [f64; 2], out: [f64; 2], prm: PyReadonlyArray1<'py, f64>,
                        sec: PyReadonlyArray2<'py, f64>, face_x: PyReadonlyArray1<'py, f64>,
                        face_g: PyReadonlyArray1<'py, f64>, wing_s: PyReadonlyArray1<'py, f64>,
                        wing_l: PyReadonlyArray1<'py, f64>, wing_r: PyReadonlyArray1<'py, f64>,
                        road_z: PyReadonlyArray1<'py, f64>)
                        -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                                     Bound<'py, PyArray1<i64>>)> {
    let pv = prm.as_slice()?;
    if pv.len() < 16 { return Err(pyo3::exceptions::PyValueError::new_err("portal_mesh: prm needs 16 values")); }
    let prm = Prm::from_slice(pv);
    let sv = sec.as_array();
    let sec: Vec<[f64; 2]> = (0..sv.shape()[0]).map(|i| [sv[[i, 0]], sv[[i, 1]]]).collect();
    let f = Frame { p, u: left, o: out };
    let m = portal(&f, &prm, &sec, face_x.as_slice()?, face_g.as_slice()?, wing_s.as_slice()?, wing_l.as_slice()?,
                   wing_r.as_slice()?, road_z.as_slice()?);
    Ok(mesh_out(py, m))
}

/// roadmesh.tunnel's cover over a tube's bare rows: `cover`. mid / left (k, 2), z / ground (k,).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn tunnel_cover_mesh<'py>(py: Python<'py>, mid: PyReadonlyArray2<'py, f64>, left: PyReadonlyArray2<'py, f64>,
                              z: PyReadonlyArray1<'py, f64>, ground: PyReadonlyArray1<'py, f64>, hw: f64, th: f64,
                              over: f64, need: f64, foot: f64, cap0: bool, cap1: bool, uv_m: f64, soil_uv_m: f64,
                              opening: PyReadonlyArray2<'py, f64>)
                              -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>,
                                           Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>)> {
    let (mv, lv) = (mid.as_array(), left.as_array());
    let k = mv.shape()[0];
    if lv.shape()[0] != k { return Err(pyo3::exceptions::PyValueError::new_err("tunnel_cover_mesh: rows differ")); }
    let m2: Vec<[f64; 2]> = (0..k).map(|i| [mv[[i, 0]], mv[[i, 1]]]).collect();
    let l2: Vec<[f64; 2]> = (0..k).map(|i| [lv[[i, 0]], lv[[i, 1]]]).collect();
    let ov = opening.as_array();
    let op: Vec<[f64; 2]> = (0..ov.shape()[0]).map(|i| [ov[[i, 0]], ov[[i, 1]]]).collect();
    let (o, _) = if op.len() >= 5 { outline(&op, th) } else { (Vec::new(), Vec::new()) };
    let m = cover(&m2, &l2, z.as_slice()?, ground.as_slice()?, hw, th, over, need, foot, (cap0, cap1), uv_m, soil_uv_m,
                  &o);
    Ok(mesh_out(py, m))
}

/// roads' portal look: `style`.
#[pyfunction]
pub fn portal_style(major: bool, th: f64, rise: f64, sides: f64) -> i64 { style(major, th, rise, sides) }

#[cfg(test)]
mod tests {
    use super::*;

    fn sec(hw: f64, th: f64) -> Vec<[f64; 2]> {
        let wall = 4.6 * th / 6.8;
        let mut v = vec![[hw - 1.2, 0.0], [hw, 0.0]];
        for i in 0..9 {
            let a = std::f64::consts::PI * i as f64 / 8.0;
            v.push([a.dcos() * hw, wall + a.dsin() * (th - wall)]);
        }
        v.push([-hw, 0.0]);
        v.push([-hw + 1.2, 0.0]);
        v
    }

    fn prm() -> Prm {
        Prm { th: 6.8, hw: 5.0, collar_out: 0.6, rake: 2.0, collar_t: 0.5, wall_t: 0.6, parapet: 0.4, side: 3.0,
              top_min: 0.9, top_max: 4.0, foot: 1.0, end_min: 1.0, wing_t: 0.5, wing_flare: 0.25, wing_low: 0.6,
              uv_m: 4.0, side_r: 3.0, flat: 0.5, inner_from: 0.0 }
    }

    fn frame() -> Frame { Frame { p: [10.0, 20.0, 100.0], u: [0.0, 1.0], o: [1.0, 0.0] } }

    /// Every triangle of a face group looks the way it should: the front out of the tube, the back into the hill.
    #[test]
    fn headwall_faces_out_and_stays_clear_of_the_opening() {
        let (p, f) = (prm(), frame());
        let m = portal(&f, &p, &sec(5.0, 6.8), &[-8.0, 0.0, 8.0], &[9.0, 9.0, 9.0], &[], &[], &[], &[]);
        assert!(!m.t.is_empty());
        let mut front_area = 0.0;
        for (t, _) in m.t.iter().zip(&m.m) {
            let (a, b, c) = (m.p[t[0] as usize], m.p[t[1] as usize], m.p[t[2] as usize]);
            let n = cross(sub(b, a), sub(c, a));
            // a triangle in the mouth's plane (x = 10) facing out: none of it inside the opening
            if (a[0] - 10.0).abs() < 1e-9 && (b[0] - 10.0).abs() < 1e-9 && (c[0] - 10.0).abs() < 1e-9 {
                assert!(n[0] > 0.0, "front faces out");
                front_area += n[0] / 2.0;
                let cen = [(a[1] + b[1] + c[1]) / 3.0 - 20.0, (a[2] + b[2] + c[2]) / 3.0 - 100.0];
                assert!(!(cen[0].abs() < 4.0 && cen[1] > 0.2 && cen[1] < 4.4), "wall inside the opening at {:?}", cen);
            }
        }
        // the wall's area: its outline less the opening (16 m wide, 1 m under the floor to 9.4 m)
        let whole = 16.0 * (1.0 + 9.4);
        assert!(front_area > whole - 80.0 && front_area < whole - 50.0, "{}", front_area);
    }

    #[test]
    fn top_follows_the_ground_and_slopes_to_low_ends() {
        let p = prm();
        // ground high over the tube (a hillside): parapet over it, capped at th + top_max
        let t = headwall_top(&p, &[-8.0, 0.0, 8.0], &[8.0, 9.0, 20.0]);
        assert!((t[0].1 - 8.4).abs() < 1e-9 && (t[t.len() - 1].1 - 10.8).abs() < 1e-9, "{:?}", t);
        // no ground (a portal standing in the open): over the crown in the middle, its ends low
        let t = headwall_top(&p, &[-8.0, 0.0, 8.0], &[0.0, 0.0, 0.0]);
        let mid = t.iter().find(|q| q.0 == 0.0).unwrap().1;
        assert!((mid - 7.7).abs() < 1e-9 && (t[0].1 - 1.0).abs() < 1e-9, "{:?}", t);
        assert!(t.windows(2).all(|w| w[1].0 > w[0].0));
    }

    #[test]
    fn wings_come_down_with_their_ground_and_end() {
        let (p, f) = (prm(), frame());
        let s = [3.0, 6.0, 9.0, 12.0, 15.0];
        let none = portal(&f, &p, &sec(5.0, 6.8), &[-8.0, 8.0], &[6.0, 6.0], &[], &[], &[], &[]);
        let both = portal(&f, &p, &sec(5.0, 6.8), &[-8.0, 8.0], &[6.0, 6.0], &s, &[5.0, 4.0, 3.0, 0.2, 0.0],
                          &[0.0; 5], &[0.0; 5]);
        assert!(both.t.len() > none.t.len());
        // the left wing's farthest point: before the station where its ground is down (12 m), no higher than its start
        let far = both.p[none.p.len()..].iter().map(|q| q[0] - 10.0).fold(0.0, f64::max);
        assert!(far > 9.0 && far < 12.0, "{}", far);
        let hi = both.p[none.p.len()..].iter().map(|q| q[2] - 100.0).fold(0.0, f64::max);
        assert!(hi <= 6.4 + 1e-9, "{}", hi);
        // the right side (ground at the road's level out there): the headwall's end comes down within the first step
        let far_r = both.p[none.p.len()..].iter().filter(|q| q[1] - 20.0 < 0.0).map(|q| q[0] - 10.0).fold(0.0, f64::max);
        assert!(far_r < 3.0 + 1e-9, "{}", far_r);
    }

    #[test]
    fn cover_only_over_bare_rows() {
        let mid: Vec<[f64; 2]> = (0..10).map(|i| [i as f64 * 5.0, 0.0]).collect();
        let left = vec![[0.0, 1.0]; 10];
        let z = vec![0.0; 10];
        let mut g = vec![20.0; 10];
        let (o, _) = outline(&sec(5.0, 6.8), 6.8);
        assert!(cover(&mid, &left, &z, &g, 7.0, 6.8, 0.3, 0.5, 1.0, (false, false), 4.0, 1.0, &o).t.is_empty());
        g[0] = 3.0;
        g[1] = 5.0;
        let m = cover(&mid, &left, &z, &g, 7.0, 6.8, 0.3, 0.5, 1.0, (false, false), 4.0, 1.0, &o);
        // rows 0..2 (a row past the bare ones): 2 spans x (2 sides + top) x 2 triangles, and the cap inside the hill
        let caps: Vec<usize> = (0..m.t.len()).filter(|&q| m.t[q].iter().all(|&v| (m.p[v as usize][0] - 10.0).abs() < 1e-9))
            .collect();
        assert_eq!(m.t.len() - caps.len(), 2 * 3 * 2);
        assert!(!caps.is_empty());
        assert!(m.p.iter().all(|p| p[0] <= 10.0 + 1e-9));
        let soil = m.m.iter().filter(|&&c| c == M_SOIL).count();
        assert_eq!(soil, 4);
        // the cap is the box's end less the tube's opening: nothing of it over the road inside the tube (a wall
        // across the tunnel)
        for &q in &caps {
            let t = m.t[q];
            let c = [(m.p[t[0] as usize][1] + m.p[t[1] as usize][1] + m.p[t[2] as usize][1]) / 3.0,
                     (m.p[t[0] as usize][2] + m.p[t[1] as usize][2] + m.p[t[2] as usize][2]) / 3.0];
            assert!(!(c[0].abs() < 4.5 && c[1] > 0.1 && c[1] < 4.0), "cap inside the opening at {:?}", c);
        }
        let area: f64 = caps.iter().map(|&q| {
            let t = m.t[q];
            let (a, b, c) = (m.p[t[0] as usize], m.p[t[1] as usize], m.p[t[2] as usize]);
            ((b[1] - a[1]) * (c[2] - a[2]) - (c[1] - a[1]) * (b[2] - a[2])).abs() / 2.0
        }).sum();
        // 14 m x (1 + 7.1) m less the opening (~10 m x 6 m)
        assert!(area > 40.0 && area < 75.0, "{}", area);
    }

    #[test]
    fn styles() {
        assert_eq!(style(true, 6.8, 10.0, 0.5), 0);     // a motorway bore into a mountain
        assert_eq!(style(false, 6.8, 10.0, 0.5), 2);    // a lane's
        assert_eq!(style(true, 6.8, 1.0, 0.0), 1);      // under flat ground
        assert_eq!(style(true, 6.8, 10.0, 4.0), 0);     // out of a rock cut: still a hood
        assert_eq!(style(true, 6.8, 10.0, 9.5), 1);     // out of a trough deeper than the opening
    }
}
