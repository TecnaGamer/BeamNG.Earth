//! Raised sidewalks (server/sidewalks.py Builder): the runs and corner pieces of a road tile swept along the curb
//! profile into one mesh, with the lean collision mesh beside it. The Python's arithmetic and output order (its
//! numpy operations one by one), so a tile's shapes stay as they were; the Python Builder gathers the pieces and
//! calls this once (it was the larger half of a road geotile's skin: ~70 ms of ~165 in Fells Point, 2026-10-29).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const CURB_H: f64 = 0.15;
const SINK: f64 = 0.10;
const BACK_RUN: f64 = 2.0;
const BATTER: f64 = 0.02;
const NOSE: f64 = 0.025;
const NOSE_W: f64 = 0.045;
const CURB_W: f64 = 0.15;
const CROSS_SLOPE: f64 = 0.015;
const CURB_REPEAT_M: f64 = 9.0;
const SLAB_REPEAT_M: f64 = 45.0;
const BRICK: i64 = 3;
const BRICK_REPEAT_M: f64 = 1.2;
const RAMP_RUN: f64 = 1.8;
const RAMP_FLARE: f64 = 0.6;
const RAMP_H: f64 = 0.01;
const TACTILE_REPEAT_M: f64 = 0.6;
const PAD_W: f64 = 0.6;
const NARROW_EDGE_M: f64 = 0.05;
const LEAN: [usize; 5] = [0, 2, 5, 6, 7];
const LEAN_TOL_M: f64 = 0.03;

fn v_band_top() -> f64 { 1.0 - 1.0 / 512.0 }
fn v_light_low() -> f64 { 1.0 - 40.0 / 512.0 }
fn v_band_low() -> f64 { 1.0 - 63.0 / 512.0 }
const V_SLAB_IN: f64 = 1.0 - 0.195;
const V_SLAB_OUT: f64 = 1.0 - 0.005;

/// (from, to, material, v from, v to) of the full profile's strips (sidewalks.STRIPS).
fn strips() -> Vec<(usize, usize, i64, f64, f64)> {
    let sl = (RAMP_RUN - CURB_W) / (2.5 - CURB_W);
    let sp = PAD_W / (2.5 - CURB_W);
    let mid = V_SLAB_IN + sl * (V_SLAB_OUT - V_SLAB_IN);
    let pad = V_SLAB_IN + sp * (V_SLAB_OUT - V_SLAB_IN);
    vec![(0, 1, 0, v_band_low(), v_light_low()), (1, 2, 0, v_light_low(), v_band_top() - 0.02),
         (2, 3, 0, v_band_top() - 0.02, v_band_top()), (3, 4, 1, V_SLAB_IN, pad), (4, 5, 1, pad, mid),
         (5, 6, 1, mid, V_SLAB_OUT), (6, 7, 0, v_light_low(), v_band_low())]
}

/// sidewalks.profile: the 8 cross-section points (offset out, height) for curb height hr.
fn profile(h: f64, w: f64, hr: f64) -> [[f64; 2]; 8] {
    let f = if h > 0.0 { hr / h } else { 0.0 };
    let rr = (CURB_W + NARROW_EDGE_M).max(RAMP_RUN.min(w - NARROW_EDGE_M));
    let zr = h + rr * CROSS_SLOPE;
    let xp = CURB_W + PAD_W.min((rr - CURB_W) / 2.0);
    let zp = hr + (zr - hr) * (xp - CURB_W) / (rr - CURB_W);
    let top = h + w * CROSS_SLOPE;
    [[0.0, -SINK], [BATTER * f, hr - NOSE * f], [NOSE_W * f, hr], [CURB_W, hr], [xp, zp], [rr, zr], [w, top],
     [w + BACK_RUN * (top + SINK), -SINK]]
}

/// np.interp(x, xp, fp) for increasing xp (numpy's formula: slope (fp[j+1] - fp[j]) / (xp[j+1] - xp[j]) times
/// (x - xp[j]) plus fp[j]; the last point's value at and past it, the first's before).
fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if n == 1 || x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    let mut lo = 0usize;
    let mut hi = n - 1;
    while hi - lo > 1 {
        let m = (lo + hi) / 2;
        if xp[m] <= x { lo = m } else { hi = m }
    }
    let slope = (fp[lo + 1] - fp[lo]) / (xp[lo + 1] - xp[lo]);
    let r = slope * (x - xp[lo]) + fp[lo];
    if r.is_nan() {      // (numpy: a zero-length step's slope is inf / nan)
        let r2 = slope * (x - xp[lo + 1]) + fp[lo + 1];
        if r2.is_nan() && fp[lo] == fp[lo + 1] { return fp[lo]; }
        return r2;
    }
    r
}

fn arclen(p: &[[f64; 3]]) -> Vec<f64> {
    let mut s = vec![0.0f64; p.len()];
    for i in 1..p.len() {
        s[i] = s[i - 1] + (p[i][0] - p[i - 1][0]).hypot(p[i][1] - p[i - 1][1]);
    }
    s
}

/// sidewalks.refine: s with points every step m round the ramps, and their ends (np.unique: sorted, distinct).
fn refine(s: &[f64], ramps: &[(f64, f64)], step: f64, margin: f64) -> Vec<f64> {
    let last = s[s.len() - 1];
    let mut all: Vec<f64> = s.to_vec();
    for &(a, b) in ramps {
        let start = (a - RAMP_FLARE - margin).max(0.0);
        let stop = last.min(b + RAMP_FLARE + margin);
        let n = ((stop - start) / step).ceil();
        if n > 0.0 {
            for i in 0..n as usize {
                all.push(start + i as f64 * step);
            }
        }
    }
    for &(a, b) in ramps {
        for v in [a, b, a - RAMP_FLARE, b + RAMP_FLARE] {
            if 0.0 < v && v < last {
                all.push(v);
            }
        }
    }
    all.sort_by(|x, y| x.partial_cmp(y).unwrap());
    all.dedup();
    all
}

/// sidewalks.curb_heights.
fn curb_heights(s: &[f64], ramps: &[(f64, f64)], h: f64) -> Vec<f64> {
    let mut hr = vec![h; s.len()];
    for &(s0, s1) in ramps {
        for (i, &v) in s.iter().enumerate() {
            let d = (s0 - v).max(v - s1);
            let f = (d / RAMP_FLARE).clamp(0.0, 1.0);
            hr[i] = hr[i].min(RAMP_H + f * (h - RAMP_H));
        }
    }
    hr
}

/// sidewalks.simplify (Douglas-Peucker over points of dimension d, both ends kept): the indices kept.
fn simplify(pts: &[Vec<f64>], tol: f64) -> Vec<usize> {
    let k = pts.len();
    if k <= 2 {
        return (0..k).collect();
    }
    let d = pts[0].len();
    let mut keep = vec![false; k];
    keep[0] = true;
    keep[k - 1] = true;
    let mut stack = vec![(0usize, k - 1)];
    while let Some((a, b)) = stack.pop() {
        if b - a < 2 {
            continue;
        }
        let seg: Vec<f64> = (0..d).map(|c| pts[b][c] - pts[a][c]).collect();
        let l2: f64 = seg.iter().map(|v| v * v).sum();
        let mut best = (0usize, -1.0f64);
        for i in a + 1..b {
            let rel: Vec<f64> = (0..d).map(|c| pts[i][c] - pts[a][c]).collect();
            let d2 = if l2 > 0.0 {
                let t = (rel.iter().zip(&seg).map(|(r, s)| r * s).sum::<f64>() / l2).clamp(0.0, 1.0);
                rel.iter().zip(&seg).map(|(r, s)| { let e = r - t * s; e * e }).sum::<f64>()
            } else {
                rel.iter().map(|r| r * r).sum()
            };
            if d2 > best.1 {
                best = (i, d2);
            }
        }
        if best.1 > tol * tol {
            keep[best.0] = true;
            stack.push((a, best.0));
            stack.push((best.0, b));
        }
    }
    (0..k).filter(|&i| keep[i]).collect()
}

#[derive(Default)]
struct Mesh {
    p: Vec<[f64; 3]>,
    uv: Vec<[f64; 2]>,
    t: Vec<[i64; 3]>,
    m: Vec<i64>,
}

fn cross3(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}
fn sub3(a: [f64; 3], b: [f64; 3]) -> [f64; 3] { [a[0] - b[0], a[1] - b[1], a[2] - b[2]] }

/// sidewalks._orient: triangle (local indices into pts) flipped when its normal points against want.
fn orient(t: [i64; 3], pts: &[[f64; 3]], want: [f64; 3]) -> [i64; 3] {
    let (a, b, c) = (pts[t[0] as usize], pts[t[1] as usize], pts[t[2] as usize]);
    let n = cross3(sub3(b, a), sub3(c, a));
    if n[0] * want[0] + n[1] * want[1] + n[2] * want[2] < 0.0 { [t[2], t[1], t[0]] } else { t }
}

impl Mesh {
    fn add(&mut self, p: Vec<[f64; 3]>, uv: Vec<[f64; 2]>) -> i64 {
        let k = self.p.len() as i64;
        self.p.extend(p);
        self.uv.extend(uv);
        k
    }

    /// Builder._strips.
    #[allow(clippy::too_many_arguments)]
    fn strips(&mut self, ring: &[Vec<[f64; 3]>], prof: &[Vec<[f64; 2]>], out: &[[f64; 2]], s: &[f64],
              strips: &[(usize, usize, i64, f64, f64)], tactile: Option<&[bool]>, slab: i64) {
        let k = ring.len();
        for &(a, b, mat0, va, vb) in strips {
            let rep = if mat0 == 0 { CURB_REPEAT_M } else { SLAB_REPEAT_M };
            let mut mat = mat0;
            let mut p: Vec<[f64; 3]> = Vec::with_capacity(2 * k);
            for r in ring { p.push(r[a]); }
            for r in ring { p.push(r[b]); }
            let mut uv: Vec<[f64; 2]> = Vec::with_capacity(2 * k);
            if mat0 == 1 && slab == BRICK {
                for i in 0..k { uv.push([s[i] / BRICK_REPEAT_M, prof[i][a][0] / BRICK_REPEAT_M]); }
                for i in 0..k { uv.push([s[i] / BRICK_REPEAT_M, prof[i][b][0] / BRICK_REPEAT_M]); }
                mat = BRICK;
            } else {
                for &v in s.iter().take(k) { uv.push([v / rep, va]); }
                for &v in s.iter().take(k) { uv.push([v / rep, vb]); }
            }
            let mut mats = vec![mat; k - 1];
            if let (Some(tac), 3) = (tactile, a) {
                for (i, &on) in tac.iter().enumerate() {
                    if on { mats[i] = 2; }
                }
                for i in 0..k {
                    let on = if i < k - 1 { tac[i] } else { tac[k - 2] };
                    if on {
                        uv[i][0] = s[i] / TACTILE_REPEAT_M;
                        uv[k + i][0] = s[i] / TACTILE_REPEAT_M;
                    }
                }
            }
            let local = p.clone();
            let base = self.add(p, uv);
            let kk = k as i64;
            let mut tris: Vec<[i64; 3]> = Vec::with_capacity(2 * (k - 1));
            for i in 0..kk - 1 { tris.push([i, kk + i + 1, i + 1]); }
            for i in 0..kk - 1 { tris.push([i, kk + i, kk + i + 1]); }
            let mut want = Vec::with_capacity(k - 1);
            for i in 0..k - 1 {
                let dp0 = 0.5 * ((prof[i][b][0] - prof[i][a][0]) + (prof[i + 1][b][0] - prof[i + 1][a][0]));
                let dp1 = 0.5 * ((prof[i][b][1] - prof[i][a][1]) + (prof[i + 1][b][1] - prof[i + 1][a][1]));
                want.push([-dp1 * out[i][0], -dp1 * out[i][1], dp0]);
            }
            for (j, tr) in tris.iter().enumerate() {
                let t = orient(*tr, &local, want[j % (k - 1)]);
                self.t.push([t[0] + base, t[1] + base, t[2] + base]);
            }
            for _ in 0..2 { self.m.extend(mats.iter().copied()); }
        }
    }

    /// The ring (k x profile points, minus origin) and per-point profiles of a curb line.
    fn sections(curb: &[[f64; 3]], out: &[[f64; 2]], w: f64, h: f64, hr: &[f64], origin: [f64; 3])
                -> (Vec<Vec<[f64; 3]>>, Vec<Vec<[f64; 2]>>) {
        let mut ring = Vec::with_capacity(curb.len());
        let mut prof = Vec::with_capacity(curb.len());
        for (i, c) in curb.iter().enumerate() {
            let pr = profile(h, w, hr[i]);
            ring.push(pr.iter().map(|q| [c[0] + out[i][0] * q[0] - origin[0], c[1] + out[i][1] * q[0] - origin[1],
                                         c[2] + q[1] - origin[2]]).collect());
            prof.push(pr.to_vec());
        }
        (ring, prof)
    }

    /// Builder.add_run.
    #[allow(clippy::too_many_arguments)]
    fn run(&mut self, curb0: &[[f64; 3]], out0: &[[f64; 2]], w: f64, h: f64, caps: (bool, bool),
           ramps: &[(f64, f64)], slab: i64, lean: bool, origin: [f64; 3]) {
        if curb0.len() < 2 {
            return;
        }
        let mut curb = curb0.to_vec();
        let mut out = out0.to_vec();
        let mut s = arclen(&curb);
        if !ramps.is_empty() {
            let s2 = refine(&s, ramps, 0.5, 1.0);
            let col = |c: usize| -> Vec<f64> { curb.iter().map(|p| p[c]).collect() };
            let (cx, cy, cz) = (col(0), col(1), col(2));
            let ox: Vec<f64> = out.iter().map(|p| p[0]).collect();
            let oy: Vec<f64> = out.iter().map(|p| p[1]).collect();
            curb = s2.iter().map(|&v| [interp(v, &s, &cx), interp(v, &s, &cy), interp(v, &s, &cz)]).collect();
            out = s2.iter().map(|&v| [interp(v, &s, &ox), interp(v, &s, &oy)]).collect();
            s = s2;
        }
        let mut hr = curb_heights(&s, ramps, h);
        if lean {
            let pts: Vec<Vec<f64>> = (0..curb.len()).map(|i| vec![curb[i][0], curb[i][1], curb[i][2], hr[i]]).collect();
            let keep = simplify(&pts, LEAN_TOL_M);
            curb = keep.iter().map(|&i| curb[i]).collect();
            out = keep.iter().map(|&i| out[i]).collect();
            s = keep.iter().map(|&i| s[i]).collect();
            hr = keep.iter().map(|&i| hr[i]).collect();
        }
        let k = curb.len();
        let (mut ring, mut prof) = Mesh::sections(&curb, &out, w, h, &hr, origin);
        if lean {
            ring = ring.iter().map(|r| LEAN.iter().map(|&j| r[j]).collect()).collect();
            prof = prof.iter().map(|r| LEAN.iter().map(|&j| r[j]).collect()).collect();
            let ls: Vec<(usize, usize, i64, f64, f64)> = (0..LEAN.len() - 1).map(|i| (i, i + 1, 0, 0.0, 0.0)).collect();
            self.strips(&ring, &prof, &out, &s, &ls, None, 1);
        } else {
            let low: Vec<bool> = hr.iter().map(|&v| v < h - 1e-6).collect();
            let tac: Option<Vec<bool>> = if low.iter().any(|&v| v) {
                Some((0..k - 1).map(|i| low[i] || low[i + 1]).collect())
            } else { None };
            self.strips(&ring, &prof, &out, &s, &strips(), tac.as_deref(), slab);
        }
        for (end, sgn, cap) in [(0usize, -1.0f64, caps.0), (k - 1, 1.0, caps.1)] {
            if !cap {
                continue;
            }
            let p: Vec<[f64; 3]> = ring[end].clone();
            let pr = &prof[end];
            let uv: Vec<[f64; 2]> = pr.iter().map(|q| [q[0] / CURB_REPEAT_M,
                v_band_low() + (q[1] + SINK) / (h + SINK + 0.05) * (v_light_low() - v_band_low())]).collect();
            let local = p.clone();
            let base = self.add(p, uv);
            let a = curb[(end + 1).min(k - 1)];
            let b = curb[if end >= 1 { end - 1 } else { 0 }];
            let (tx, ty) = (a[0] - b[0], a[1] - b[1]);
            let l = tx.hypot(ty).max(1e-9);
            let want = [tx / l * sgn, ty / l * sgn, 0.0 * sgn];
            for j in 1..pr.len() as i64 - 1 {
                let t = orient([0, j, j + 1], &local, want);
                self.t.push([t[0] + base, t[1] + base, t[2] + base]);
                self.m.push(0);
            }
        }
    }

    /// Builder.add_corner.
    #[allow(clippy::too_many_arguments)]
    fn corner(&mut self, arc0: &[[f64; 3]], inward0: &[[f64; 2]], outer: &[[f64; 3]], h: f64,
              ramps: &[(f64, f64)], lean: bool, origin: [f64; 3]) {
        let mut arc = arc0.to_vec();
        let mut inward = inward0.to_vec();
        let mut s = arclen(&arc);
        if !ramps.is_empty() {
            let s2 = refine(&s, ramps, 0.5, 1.0);
            let col = |c: usize| -> Vec<f64> { arc.iter().map(|p| p[c]).collect() };
            let (cx, cy, cz) = (col(0), col(1), col(2));
            let ix: Vec<f64> = inward.iter().map(|p| p[0]).collect();
            let iy: Vec<f64> = inward.iter().map(|p| p[1]).collect();
            arc = s2.iter().map(|&v| [interp(v, &s, &cx), interp(v, &s, &cy), interp(v, &s, &cz)]).collect();
            inward = s2.iter().map(|&v| {
                let (x, y) = (interp(v, &s, &ix), interp(v, &s, &iy));
                let n = x.hypot(y).max(1e-9);
                [x / n, y / n]
            }).collect();
            s = s2;
        }
        let mut hr = curb_heights(&s, ramps, h);
        if lean {
            let pts: Vec<Vec<f64>> = (0..arc.len()).map(|i| vec![arc[i][0], arc[i][1], arc[i][2], hr[i]]).collect();
            let keep = simplify(&pts, LEAN_TOL_M);
            arc = keep.iter().map(|&i| arc[i]).collect();
            inward = keep.iter().map(|&i| inward[i]).collect();
            s = keep.iter().map(|&i| s[i]).collect();
            hr = keep.iter().map(|&i| hr[i]).collect();
        }
        let k = arc.len();
        let (ring, prof) = Mesh::sections(&arc, &inward, 2.5, h, &hr, origin);
        if lean {
            self.strips(&ring, &prof, &inward, &s, &[(0, 3, 0, 0.0, 0.0)], None, 1);
        } else {
            let cs: Vec<(usize, usize, i64, f64, f64)> = strips()[..3].to_vec();
            self.strips(&ring, &prof, &inward, &s, &cs, None, 1);
        }
        let mut poly: Vec<[f64; 3]> = ring.iter().map(|r| r[3]).collect();
        let lift = h + 2.5 * CROSS_SLOPE;
        let out: Vec<[f64; 3]> = outer.iter().map(|o| [o[0] - origin[0], o[1] - origin[1], o[2] - origin[2] + lift])
            .collect();
        poly.extend(out.iter().copied());
        let uv: Vec<[f64; 2]> = poly.iter().map(|q| [q[0] / SLAB_REPEAT_M, q[1] / (SLAB_REPEAT_M / 4.0)]).collect();
        let local = poly.clone();
        let base = self.add(poly.clone(), uv);
        let m = poly.len();
        let fan: Vec<[i64; 3]> = if out.len() == 3 {
            let o = k + 1;
            (0..m).filter(|&a| a != o && (a + 1) % m != o).map(|a| [o as i64, a as i64, ((a + 1) % m) as i64]).collect()
        } else {
            let xy: Vec<(f64, f64)> = poly.iter().map(|q| (q[0], q[1])).collect();
            let mut area = 0.0;
            for i in 0..m {
                let j = (i + 1) % m;
                area += xy[i].0 * xy[j].1 - xy[j].0 * xy[i].1;
            }
            let order: Vec<usize> = if area > 0.0 { (0..m).collect() } else { (0..m).rev().collect() };
            let pts: Vec<(f64, f64)> = order.iter().map(|&i| xy[i]).collect();
            crate::ear_clip(&pts).iter().map(|t| [order[t[0] as usize] as i64, order[t[1] as usize] as i64,
                                                  order[t[2] as usize] as i64]).collect()
        };
        for t in &fan {
            let tt = orient(*t, &local, [0.0, 0.0, 1.0]);
            self.t.push([tt[0] + base, tt[1] + base, tt[2] + base]);
            self.m.push(1);
        }
        let (mut cx, mut cy) = (0.0, 0.0);
        for q in &poly {
            cx += q[0];
            cy += q[1];
        }
        let c = [cx / m as f64, cy / m as f64];
        let drop = h + SINK + 2.5 * CROSS_SLOPE;
        for w2 in out.windows(2) {
            let (a, b) = (w2[0], w2[1]);
            let t = [b[0] - a[0], b[1] - a[1]];
            let l = t[0].hypot(t[1]).max(1e-9);
            let mut nrm = [t[1] / l, -t[0] / l];
            let mid = [0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])];
            if nrm[0] * (mid[0] - c[0]) + nrm[1] * (mid[1] - c[1]) < 0.0 {
                nrm = [-nrm[0], -nrm[1]];
            }
            let run = [nrm[0] * BACK_RUN * drop, nrm[1] * BACK_RUN * drop, -drop];
            let p = vec![a, b, [b[0] + run[0], b[1] + run[1], b[2] + run[2]], [a[0] + run[0], a[1] + run[1], a[2] + run[2]]];
            let len = (b[0] - a[0]).hypot(b[1] - a[1]) / CURB_REPEAT_M;
            let uv = vec![[0.0, v_light_low()], [len, v_light_low()], [len, v_band_low()], [0.0, v_band_low()]];
            let want = [mid[0] - c[0], mid[1] - c[1], 0.0];
            let local = p.clone();
            let base = self.add(p, uv);
            for q in [[0i64, 1, 2], [0, 2, 3]] {
                let tt = orient(q, &local, want);
                self.t.push([tt[0] + base, tt[1] + base, tt[2] + base]);
                self.m.push(0);
            }
        }
    }
}

fn pts3(a: &numpy::ndarray::ArrayView2<'_, f64>, r: std::ops::Range<usize>) -> Vec<[f64; 3]> {
    r.map(|i| [a[[i, 0]], a[[i, 1]], if a.ncols() > 2 { a[[i, 2]] } else { 0.0 }]).collect()
}
fn pts2(a: &numpy::ndarray::ArrayView2<'_, f64>, r: std::ops::Range<usize>) -> Vec<[f64; 2]> {
    r.map(|i| [a[[i, 0]], a[[i, 1]]]).collect()
}

type MeshOut<'py> = (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                     Bound<'py, PyArray1<i64>>);

fn mesh_out<'py>(py: Python<'py>, m: Mesh) -> MeshOut<'py> {
    let (n, t) = (m.p.len(), m.t.len());
    (numpy::ndarray::Array2::from_shape_vec((n, 3), m.p.into_iter().flatten().collect()).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((t, 3), m.t.into_iter().flatten().collect()).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((n, 2), m.uv.into_iter().flatten().collect()).unwrap().into_pyarray(py),
     m.m.into_pyarray(py))
}

/// sidewalks.Builder.arrays: the pieces (kind 0 a run, 1 a corner), in order, swept into (P (n, 3) relative to origin,
/// T (m, 3), UV (n, 2), material (m,)), and the lean collision mesh's the same (collision: else empty). Per piece i:
/// its points pts[off[i]..off[i+1]] (a run's curb line, a corner's arc), dirs the same rows (toward_out / inward),
/// a corner's outer points outer[ooff[i]..ooff[i+1]], ramps[roff[i]..roff[i+1]] (s0, s1), w, h, caps (2 per piece),
/// slab.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn sidewalk_mesh<'py>(py: Python<'py>, kind: PyReadonlyArray1<'py, i64>, pts: PyReadonlyArray2<'py, f64>,
                          dirs: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                          outer: PyReadonlyArray2<'py, f64>, ooff: PyReadonlyArray1<'py, i64>,
                          ramps: PyReadonlyArray2<'py, f64>, roff: PyReadonlyArray1<'py, i64>,
                          w: PyReadonlyArray1<'py, f64>, h: PyReadonlyArray1<'py, f64>,
                          caps: PyReadonlyArray2<'py, bool>, slab: PyReadonlyArray1<'py, i64>,
                          origin: (f64, f64, f64), collision: bool) -> PyResult<(MeshOut<'py>, MeshOut<'py>)> {
    let (kind, pts, dirs, off) = (kind.as_array(), pts.as_array(), dirs.as_array(), off.as_array());
    let (outer, ooff, ramps, roff) = (outer.as_array(), ooff.as_array(), ramps.as_array(), roff.as_array());
    let (w, h, caps, slab) = (w.as_array(), h.as_array(), caps.as_array(), slab.as_array());
    let n = kind.len();
    if off.len() != n + 1 || ooff.len() != n + 1 || roff.len() != n + 1 || w.len() != n || h.len() != n
        || caps.nrows() != n || slab.len() != n || dirs.nrows() != pts.nrows() {
        return Err(PyValueError::new_err("sidewalk_mesh: inconsistent piece arrays"));
    }
    let o = [origin.0, origin.1, origin.2];
    let (mut full, mut lean) = (Mesh::default(), Mesh::default());
    for i in 0..n {
        let (a, b) = (off[i] as usize, off[i + 1] as usize);
        let p = pts3(&pts, a..b);
        let d = pts2(&dirs, a..b);
        let rr: Vec<(f64, f64)> = (roff[i] as usize..roff[i + 1] as usize).map(|j| (ramps[[j, 0]], ramps[[j, 1]])).collect();
        if kind[i] == 0 {
            let c = (caps[[i, 0]], caps[[i, 1]]);
            if collision {
                lean.run(&p, &d, w[i], h[i], c, &rr, 1, true, o);
            }
            full.run(&p, &d, w[i], h[i], c, &rr, slab[i], false, o);
        } else {
            let ou = pts3(&outer, ooff[i] as usize..ooff[i + 1] as usize);
            if collision {
                lean.corner(&p, &d, &ou, h[i], &rr, true, o);
            }
            full.corner(&p, &d, &ou, h[i], &rr, false, o);
        }
    }
    Ok((mesh_out(py, full), mesh_out(py, lean)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interp_like_numpy() {
        let xp = [0.0, 1.0, 3.0];
        let fp = [0.0, 10.0, 30.0];
        assert_eq!(interp(-1.0, &xp, &fp), 0.0);
        assert_eq!(interp(2.0, &xp, &fp), 20.0);
        assert_eq!(interp(3.0, &xp, &fp), 30.0);
        assert_eq!(interp(1.0, &xp, &fp), 10.0);
    }

    #[test]
    fn a_straight_run() {
        let mut m = Mesh::default();
        let curb: Vec<[f64; 3]> = (0..5).map(|i| [i as f64 * 2.0, 0.0, 1.0]).collect();
        let out = vec![[0.0, 1.0]; 5];
        m.run(&curb, &out, 2.5, CURB_H, (true, true), &[], 1, false, [0.0, 0.0, 0.0]);
        assert_eq!(m.p.len(), 7 * 2 * 5 + 2 * 8);
        assert_eq!(m.t.len(), 7 * 2 * 4 + 2 * 6);
    }
}
