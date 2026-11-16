//! The 3D tram track in the open and on road decks (plan 046 stages D and F, the PR's port of railways.tile_mesh's tram
//! branches): a part's rails, sleepers (one grid over a turnout, reaching across to the branch), frogs and flange collider,
//! the paving of a street tram on a road bridge's deck, the road deck's height under a tram bridge (road_deck_z) and
//! whether a tram's bridge piece rides a road deck (rides_deck). railways.tile_mesh only appends what these return, in the
//! order the Python it replaces drew them: strips as vertices / triangles / uvs / a material tag per triangle (0 the rail
//! steel, 1 the collider), boxes as railways._Mesh.boxes takes them (centres, axes, heights, half sizes, a tag: 0 steel,
//! 2 sleepers). The numbers are railways.py's (STEP_M, TRAM_*, COLLIDER_*), the arithmetic numpy's in its order.
use super::lines::P2;
use super::profile::avg;
use super::switches::ray_reach;
use super::track::{open_heights, OPEN_SLEEPER_FREE_M};
use crate::dm::Dm;

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

/// railways.TRAM_RAIL_HEAD, TRAM_RAIL_H: the rail's head width and height.
pub const RAIL_HEAD: f64 = 0.075;
/// railways.BALLAST_H: the bed line lies this under the ballast top a railway's bed function returns.
pub const BALLAST_H: f64 = 0.3;
/// railways.TRAM_SLEEPER_EVERY, TRAM_SLEEPER_L, SLEEPER_W, SLEEPER_H.
pub const SLEEPER_EVERY: f64 = 0.5;
pub const SLEEPER_L: f64 = 2.2;
pub const SLEEPER_W: f64 = 0.26;
pub const SLEEPER_H: f64 = 0.2;
/// railways.COLLIDER_W, COLLIDER_DOWN.
pub const COLLIDER_W: f64 = 0.30;
pub const COLLIDER_DOWN: f64 = 0.10;
/// m a sleeper over a turnout reaches along its axis to the branch at most.
pub const SLEEPER_REACH_M: f64 = 8.0;
/// railways.DECK_NEAR_M: a tram bridge point this close to a road deck lies on it.
pub const DECK_NEAR_M: f64 = 6.0;
/// railways.FLUSH_LIFT_M and tramtrack.PAVING_EXTRA_M: the deck paving's lift over the deck surface and its width past the gauge.
pub const FLUSH_LIFT_M: f64 = 0.03;
pub const PAVING_EXTRA_M: f64 = 1.0;

pub const TAG_STEEL: u8 = 0;
pub const TAG_COLLIDER: u8 = 1;
pub const TAG_SLEEPER: u8 = 2;

type P3 = [f64; 3];

/// railways._Mesh's strips, in this crate: positions, uvs, triangles (0-based in this mesh), a tag per triangle.
#[derive(Default)]
pub struct Strips {
    pub p: Vec<P3>,
    pub uv: Vec<[f64; 2]>,
    pub t: Vec<[i64; 3]>,
    pub tag: Vec<u8>,
}

impl Strips {
    /// railways._Mesh.strip: quads between rows a and b (k each), uvs (ua | ub, v), each triangle turned to face its up (one
    /// for all, or one per row: its first k - 1 for the quads' two triangles).
    pub fn strip(&mut self, a: &[P3], b: &[P3], ua: f64, ub: f64, v: &[f64], tag: u8, up: &[P3]) {
        let k = a.len();
        if k < 2 {
            return;
        }
        let base = self.p.len() as i64;
        let p0 = self.p.len();
        self.p.extend_from_slice(a);
        self.p.extend_from_slice(b);
        for i in 0..k {
            self.uv.push([ua, v[i]]);
        }
        for i in 0..k {
            self.uv.push([ub, v[i]]);
        }
        let kk = k as i64;
        let mut tris: Vec<[i64; 3]> = Vec::with_capacity(2 * (k - 1));
        for i in 0..kk - 1 {
            tris.push([i, kk + i, kk + i + 1]);
        }
        for i in 0..kk - 1 {
            tris.push([i, kk + i + 1, i + 1]);
        }
        for (n, tri) in tris.iter_mut().enumerate() {
            let w = if up.len() == 1 { up[0] } else { up[n % (k - 1)] };
            let (q0, q1, q2) = (self.p[p0 + tri[0] as usize], self.p[p0 + tri[1] as usize], self.p[p0 + tri[2] as usize]);
            let (e1, e2) = ([q1[0] - q0[0], q1[1] - q0[1], q1[2] - q0[2]], [q2[0] - q0[0], q2[1] - q0[1], q2[2] - q0[2]]);
            let nrm = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]];
            if nrm[0] * w[0] + nrm[1] * w[1] + nrm[2] * w[2] < 0.0 {
                tri.reverse();
            }
            self.t.push([tri[0] + base, tri[1] + base, tri[2] + base]);
            self.tag.push(tag);
        }
    }
}

/// One box set as railways._Mesh.boxes takes it: centres, the along and across axes, foot and top heights, half sizes along
/// (hx) and across (hy, one each), a tag.
pub struct Boxes {
    pub c: Vec<P2>,
    pub along: Vec<P2>,
    pub across: Vec<P2>,
    pub z0: Vec<f64>,
    pub z1: Vec<f64>,
    pub hx: f64,
    pub hy: Vec<f64>,
    pub tag: u8,
}

/// railways._arc.
fn arc(xy: &[P2]) -> Vec<f64> {
    let mut s = vec![0.0f64; xy.len()];
    for k in 1..xy.len() {
        s[k] = s[k - 1] + (xy[k][0] - xy[k - 1][0]).dhypot(xy[k][1] - xy[k - 1][1]);
    }
    s
}

/// railways._left: np.gradient's direction (one-sided at the ends), normalised, turned left.
fn left(xy: &[P2]) -> Vec<P2> {
    let n = xy.len();
    (0..n)
        .map(|i| {
            let d = if n < 2 {
                [0.0, 0.0]
            } else if i == 0 {
                [xy[1][0] - xy[0][0], xy[1][1] - xy[0][1]]
            } else if i == n - 1 {
                [xy[n - 1][0] - xy[n - 2][0], xy[n - 1][1] - xy[n - 2][1]]
            } else {
                [(xy[i + 1][0] - xy[i - 1][0]) / 2.0, (xy[i + 1][1] - xy[i - 1][1]) / 2.0]
            };
            let l = d[0].dhypot(d[1]).max(1e-9);
            [-(d[1] / l), d[0] / l]
        })
        .collect()
}

/// railways._runs: (i, j) inclusive ranges where mask is false, at least 2 long.
fn runs(mask: &[bool]) -> Vec<(usize, usize)> {
    let (mut out, mut i, n) = (Vec::new(), 0usize, mask.len());
    while i < n {
        if mask[i] {
            i += 1;
            continue;
        }
        let mut j = i;
        while j + 1 < n && !mask[j + 1] {
            j += 1;
        }
        if j > i {
            out.push((i, j));
        }
        i = j + 1;
    }
    out
}

/// np.interp(x, xp, fp) for an increasing xp.
fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    let j = xp.partition_point(|&v| v <= x) - 1;
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    slope * (x - xp[j]) + fp[j]
}

/// np.arange(start, stop, step) (start + k step for k < ceil((stop - start) / step)).
fn arange(start: f64, stop: f64, step: f64) -> Vec<f64> {
    let n = ((stop - start) / step).ceil();
    if !(n > 0.0) {
        return Vec::new();
    }
    (0..n as usize).map(|k| start + k as f64 * step).collect()
}

/// railways._sleepers for a tram (none gone): the sleepers of run i..j every SLEEPER_EVERY m from the part's start, between
/// arcs lo and hi; their top is `top` (interpolated), SLEEPER_H deep.
fn sleepers(s: &[f64], xy: &[P2], u: &[P2], top: &[f64], i: usize, j: usize, lo: f64, hi: f64) -> Option<Boxes> {
    let ss = arange((s[i].max(lo) / SLEEPER_EVERY).ceil() * SLEEPER_EVERY, s[j].min(hi), SLEEPER_EVERY);
    if ss.is_empty() {
        return None;
    }
    let xs: Vec<f64> = xy.iter().map(|p| p[0]).collect();
    let ys: Vec<f64> = xy.iter().map(|p| p[1]).collect();
    let uxs: Vec<f64> = u.iter().map(|p| p[0]).collect();
    let uys: Vec<f64> = u.iter().map(|p| p[1]).collect();
    let mut b = Boxes { c: vec![], along: vec![], across: vec![], z0: vec![], z1: vec![], hx: SLEEPER_W / 2.0, hy: vec![], tag: TAG_SLEEPER };
    for &a in &ss {
        let (ux, uy) = (interp(a, s, &uxs), interp(a, s, &uys));
        let l = ux.dhypot(uy).max(1e-9);
        let un = [ux / l, uy / l];
        let zt = interp(a, s, top) - SLEEPER_H;
        b.c.push([interp(a, s, &xs), interp(a, s, &ys)]);
        b.along.push([un[1], -un[0]]);
        b.across.push(un);
        b.z0.push(zt);
        b.z1.push(zt + SLEEPER_H);
        b.hy.push(SLEEPER_L / 2.0);
    }
    Some(b)
}

/// A turnout's information for one part (tram/track.rs open_runs: BranchInfo and Mark).
pub struct Branch {
    pub zone: f64,
    pub div: f64,
}

pub struct Mark {
    pub s0: f64,
    pub s1: f64,
    pub frog: Option<P2>,
    pub frog_s: f64,
    pub branch: Vec<P2>,
}

/// The bed line of a 3D open part (railways' zb): the ground under it smoothed (railways._avg over 2), BALLAST_H under the
/// ballast top a railway's profile has.
pub fn open_bed(zg: &[f64]) -> Vec<f64> {
    avg(zg, 2).into_iter().map(|v| v - BALLAST_H).collect()
}

/// The 3D track of one open tram part xy (every railways.STEP_M), gauge g, over the drawn ground zg at its points: cut
/// (its points not drawn: off the tile, on a crossing road), inside (its points in the tile), sw (its points in a switch:
/// railways._switch_masks), ramps at its ends, its turnout (branch: it leaves a through track; marks: turnouts on it).
/// Returns (the rails' strips, the boxes: sleepers per run then the frogs, the collider's strips).
#[allow(clippy::too_many_arguments)]
pub fn open_part(xy: &[P2], g: f64, zg: &[f64], cut: &[bool], inside: &[bool], sw: &[bool], ramp: (bool, bool),
                 branch: Option<&Branch>, marks: &[Mark]) -> (Strips, Vec<Boxes>, Strips) {
    let (mut rails, mut boxes, mut coll) = (Strips::default(), Vec::new(), Strips::default());
    let n = xy.len();
    if n < 2 || zg.len() != n || cut.len() != n || inside.len() != n || sw.len() != n {
        return (rails, boxes, coll);
    }
    let u = left(xy);
    let s = arc(xy);
    let zs = avg(zg, 2);
    let (top, rail, sleeper) = open_heights(&s, zg, &zs, ramp.0, ramp.1);
    let total = s[n - 1];
    let (lo, hi) = (if ramp.0 { OPEN_SLEEPER_FREE_M } else { 0.0 }, if ramp.1 { total - OPEN_SLEEPER_FREE_M } else { total });
    let half_g = g / 2.0 + RAIL_HEAD / 2.0;
    let up = [[0.0, 0.0, 1.0]];
    for (i, j) in runs(cut) {
        let (p, uu) = (&xy[i..=j], &u[i..=j]);
        let v: Vec<f64> = s[i..=j].iter().map(|a| a / 3.0).collect();
        let st: Vec<f64> = (i..=j).map(|k| sleeper[k].min(rail[k] - 0.01)).collect();
        let r0 = match branch {
            Some(b) => (s[i..=j].partition_point(|&a| a < b.div) as i64 - 1).max(0) as usize,
            None => 0,
        };
        if p.len() >= r0 + 2 {
            let out_l: Vec<P3> = uu[r0..].iter().map(|q| [q[0], q[1], 0.0]).collect();
            let neg_l: Vec<P3> = out_l.iter().map(|q| [-q[0], -q[1], -q[2]]).collect();
            for sg in [1.0f64, -1.0] {
                let (mut a_top, mut b_top, mut a_st, mut b_st) = (vec![], vec![], vec![], vec![]);
                for k in r0..p.len() {
                    let c = [p[k][0] + uu[k][0] * sg * half_g, p[k][1] + uu[k][1] * sg * half_g];
                    let a = [c[0] + uu[k][0] * RAIL_HEAD / 2.0, c[1] + uu[k][1] * RAIL_HEAD / 2.0];
                    let b = [c[0] - uu[k][0] * RAIL_HEAD / 2.0, c[1] - uu[k][1] * RAIL_HEAD / 2.0];
                    let rt = rail[i + k];
                    a_top.push([a[0], a[1], rt]);
                    b_top.push([b[0], b[1], rt]);
                    a_st.push([a[0], a[1], st[k]]);
                    b_st.push([b[0], b[1], st[k]]);
                }
                let vr = &v[r0..];
                rails.strip(&a_top, &b_top, 0.0, 0.02, vr, TAG_STEEL, &up);
                rails.strip(&a_st, &a_top, 0.0, 0.05, vr, TAG_STEEL, &out_l);
                rails.strip(&b_st, &b_top, 0.0, 0.05, vr, TAG_STEEL, &neg_l);
            }
        }
        let lo_s = match branch {
            Some(b) => lo.max(b.zone),
            None => lo,
        };
        if let Some(mut sp) = sleepers(&s, xy, &u, &sleeper, i, j, lo_s, hi) {
            // (the through track's sleepers over a turnout reach across to the branch: one grid)
            for mk in marks {
                let arc_s: Vec<f64> = sp
                    .c
                    .iter()
                    .map(|cc| {
                        let (mut best, mut bk) = (f64::INFINITY, 0usize);
                        for (k, q) in xy.iter().enumerate() {
                            let d = (q[0] - cc[0]).dhypot(q[1] - cc[1]);
                            if d < best {
                                best = d;
                                bk = k;
                            }
                        }
                        s[bk]
                    })
                    .collect();
                for k in 0..sp.c.len() {
                    if arc_s[k] >= mk.s0 && arc_s[k] <= mk.s1 {
                        let r = ray_reach(sp.c[k], sp.across[k], &mk.branch, SLEEPER_REACH_M);
                        if r.abs() > 0.0 {
                            sp.c[k] = [sp.c[k][0] + sp.across[k][0] * (r / 2.0), sp.c[k][1] + sp.across[k][1] * (r / 2.0)];
                            sp.hy[k] = SLEEPER_L / 2.0 + r.abs() / 2.0;     // (the last mark's reach: as the Python had it)
                        }
                    }
                }
            }
            boxes.push(sp);
        }
    }
    // a frog on the through track where a branch's inner rail crosses its stock rail
    for mk in marks {
        if let Some(fx) = mk.frog {
            let (mut best, mut k0) = (f64::INFINITY, 0usize);
            for (k, q) in xy.iter().enumerate() {
                let d = (q[0] - fx[0]).dhypot(q[1] - fx[1]);
                if d < best {
                    best = d;
                    k0 = k;
                }
            }
            let k1 = (k0 + 1).min(n - 1);
            let a = [xy[k1][0] - xy[k0.saturating_sub(1)][0], xy[k1][1] - xy[k0.saturating_sub(1)][1]];
            let l = a[0].dhypot(a[1]).max(1e-9);
            let along = [a[0] / l, a[1] / l];
            let zt = interp(mk.frog_s, &s, &rail);
            boxes.push(Boxes { c: vec![fx], along: vec![along], across: vec![[-along[1], along[0]]], z0: vec![zt - 0.05],
                               z1: vec![zt + 0.004], hx: 0.45, hy: vec![0.10], tag: TAG_STEEL });
        }
    }
    // the flange collider: none in a switch, a turnout's zone or over a mark (no wall across the other track)
    let mut gap: Vec<bool> = (0..n).map(|k| !inside[k] || sw[k]).collect();
    if let Some(b) = branch {
        for k in 0..n {
            gap[k] |= s[k] <= b.zone;
        }
    }
    for mk in marks {
        for k in 0..n {
            gap[k] |= s[k] >= mk.s0 && s[k] <= mk.s1;
        }
    }
    let gin = half_g - RAIL_HEAD / 2.0;
    for (i, j) in runs(&gap) {
        let v: Vec<f64> = s[i..=j].iter().map(|a| a / 3.0).collect();
        let out_l: Vec<P3> = u[i..=j].iter().map(|q| [q[0], q[1], 0.0]).collect();
        for sg in [1.0f64, -1.0] {
            let (mut ci_t, mut co_t, mut ci_l, mut co_l) = (vec![], vec![], vec![], vec![]);
            for k in i..=j {
                let (zt, zl) = (rail[k], top[k] - COLLIDER_DOWN);
                let ci = [xy[k][0] + u[k][0] * sg * gin, xy[k][1] + u[k][1] * sg * gin];
                let co = [xy[k][0] + u[k][0] * sg * (gin + COLLIDER_W), xy[k][1] + u[k][1] * sg * (gin + COLLIDER_W)];
                ci_t.push([ci[0], ci[1], zt]);
                co_t.push([co[0], co[1], zt]);
                ci_l.push([ci[0], ci[1], zl]);
                co_l.push([co[0], co[1], zl]);
            }
            let inner: Vec<P3> = out_l.iter().map(|q| [-sg * q[0], -sg * q[1], -sg * q[2]]).collect();
            let outer: Vec<P3> = out_l.iter().map(|q| [sg * q[0], sg * q[1], sg * q[2]]).collect();
            coll.strip(&ci_t, &co_t, 0.0, 0.02, &v, TAG_COLLIDER, &up);
            coll.strip(&ci_l, &ci_t, 0.0, 0.05, &v, TAG_COLLIDER, &inner);
            coll.strip(&co_l, &co_t, 0.0, 0.05, &v, TAG_COLLIDER, &outer);
        }
    }
    (rails, boxes, coll)
}

/// railways._tram_paving: the paved strip of a street tram on a road deck: the gauge and PAVING_EXTRA_M / 2 outside each
/// rail's centre, FLUSH_LIFT_M over the deck surface (the ballast top t less BALLAST_H), facing up.
pub fn deck_paving(p: &[P2], uu: &[P2], t: &[f64], v: &[f64], half_g: f64) -> Strips {
    let mut out = Strips::default();
    let w = half_g + PAVING_EXTRA_M / 2.0;
    let a: Vec<P3> = (0..p.len()).map(|k| [p[k][0] + uu[k][0] * w, p[k][1] + uu[k][1] * w, t[k] - BALLAST_H + FLUSH_LIFT_M]).collect();
    let b: Vec<P3> = (0..p.len()).map(|k| [p[k][0] - uu[k][0] * w, p[k][1] - uu[k][1] * w, t[k] - BALLAST_H + FLUSH_LIFT_M]).collect();
    out.strip(&a, &b, 0.0, 1.0, v, 0, &[[0.0, 0.0, 1.0]]);
    out
}

/// railways.road_deck_z: the surface height of the road deck under each point (the nearest of decks [(line, heights)] within
/// DECK_NEAR_M, interpolated along it), NaN where none.
pub fn road_deck_z(xy: &[P2], decks: &[(Vec<P2>, Vec<f64>)]) -> Vec<f64> {
    let mut best = vec![f64::INFINITY; xy.len()];
    let mut z = vec![f64::NAN; xy.len()];
    for (dxy, dz) in decks {
        if dxy.len() < 2 || dz.len() < dxy.len() {
            continue;
        }
        for (i, p) in xy.iter().enumerate() {
            let (mut dm, mut bj, mut bt) = (f64::INFINITY, usize::MAX, 0.0);
            for j in 0..dxy.len() - 1 {
                let (a, b) = (dxy[j], dxy[j + 1]);
                let d = [b[0] - a[0], b[1] - a[1]];
                let seg = (d[0] * d[0] + d[1] * d[1]).max(1e-12);
                let t = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / seg).clamp(0.0, 1.0);
                let (qx, qy) = (a[0] + t * d[0] - p[0], a[1] + t * d[1] - p[1]);
                let dist = (qx * qx + qy * qy).sqrt();
                if bj == usize::MAX || dist < dm {
                    dm = dist;
                    bj = j;
                    bt = t;
                }
            }
            if dm < best[i] && dm <= DECK_NEAR_M {
                best[i] = dm;
                z[i] = dz[bj] + bt * (dz[bj + 1] - dz[bj]);
            }
        }
    }
    z
}

/// grading.tram_rides_deck: whether a tram's bridge piece (line) lies on a road deck: at least `share` of its length, sampled
/// every `step` m, within DECK_NEAR_M of one of the decks' lines.
pub fn rides_deck(line: &[P2], decks: &[Vec<P2>], share: f64, step: f64) -> bool {
    if line.len() < 2 || decks.is_empty() {
        return false;
    }
    let s = arc(line);
    let total = s[s.len() - 1];
    if total <= 0.0 {
        return false;
    }
    let m = ((total / step) as i64 + 1).max(2) as usize;
    let dt = total / (m - 1) as f64;
    let xs: Vec<f64> = line.iter().map(|p| p[0]).collect();
    let ys: Vec<f64> = line.iter().map(|p| p[1]).collect();
    let pts: Vec<P2> = (0..m)
        .map(|k| {
            let t = if k == m - 1 { total } else { k as f64 * dt };
            [interp(t, &s, &xs), interp(t, &s, &ys)]
        })
        .collect();
    let dz: Vec<(Vec<P2>, Vec<f64>)> = decks.iter().filter(|d| d.len() >= 2).map(|d| (d.clone(), vec![0.0; d.len()])).collect();
    let z = road_deck_z(&pts, &dz);
    let on = z.iter().filter(|v| v.is_finite()).count() as f64 / m as f64;
    on >= share
}

fn pts2(a: &PyReadonlyArray2<f64>) -> Vec<P2> {
    let v = a.as_array();
    (0..v.nrows()).map(|i| [v[[i, 0]], v[[i, 1]]]).collect()
}

fn strips_py<'py>(py: Python<'py>, s: &Strips) -> PyResult<Bound<'py, PyTuple>> {
    let p: Vec<f64> = s.p.iter().flat_map(|q| *q).collect();
    let uv: Vec<f64> = s.uv.iter().flat_map(|q| *q).collect();
    let t: Vec<i64> = s.t.iter().flat_map(|q| *q).collect();
    PyTuple::new(py, [
        numpy::ndarray::Array2::from_shape_vec((s.p.len(), 3), p).unwrap().into_pyarray(py).into_any(),
        numpy::ndarray::Array2::from_shape_vec((s.t.len(), 3), t).unwrap().into_pyarray(py).into_any(),
        numpy::ndarray::Array2::from_shape_vec((s.uv.len(), 2), uv).unwrap().into_pyarray(py).into_any(),
        numpy::ndarray::Array1::from(s.tag.clone()).into_pyarray(py).into_any(),
    ])
}

/// The 3D track of one open tram part (see open_part): ((P, T, UV, tag) the rails, [boxes], (P, T, UV, tag) the collider). A box
/// is (centres (k, 2), along (k, 2), across (k, 2), z0 (k,), z1 (k,), hx, hy (k,), tag). branch: None or (zone, div); marks:
/// [(s0, s1, frog (x, y) or None, frog_s, branch line (m, 2))].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tram_open_mesh<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, gauge: f64, zg: PyReadonlyArray1<'py, f64>,
                           cut: Vec<bool>, inside: Vec<bool>, sw: Vec<bool>, ramp_start: bool, ramp_end: bool,
                           branch: Option<(f64, f64)>,
                           marks: Vec<(f64, f64, Option<(f64, f64)>, f64, PyReadonlyArray2<'py, f64>)>)
                           -> PyResult<Bound<'py, PyTuple>> {
    let p = pts2(&xy);
    let z = zg.as_array().to_vec();
    if z.len() != p.len() || cut.len() != p.len() || inside.len() != p.len() || sw.len() != p.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_open_mesh: a value per point"));
    }
    let br = branch.map(|(zone, div)| Branch { zone, div });
    let mk: Vec<Mark> = marks
        .iter()
        .map(|(s0, s1, f, fs, b)| Mark { s0: *s0, s1: *s1, frog: f.map(|q| [q.0, q.1]), frog_s: *fs, branch: pts2(b) })
        .collect();
    let (rails, boxes, coll) = py.detach(|| open_part(&p, gauge, &z, &cut, &inside, &sw, (ramp_start, ramp_end), br.as_ref(), &mk));
    let bl = PyList::empty(py);
    for b in &boxes {
        let k = b.c.len();
        let f = |v: &Vec<P2>| numpy::ndarray::Array2::from_shape_vec((k, 2), v.iter().flat_map(|q| *q).collect()).unwrap().into_pyarray(py);
        bl.append(PyTuple::new(py, [
            f(&b.c).into_any(), f(&b.along).into_any(), f(&b.across).into_any(),
            numpy::ndarray::Array1::from(b.z0.clone()).into_pyarray(py).into_any(),
            numpy::ndarray::Array1::from(b.z1.clone()).into_pyarray(py).into_any(),
            b.hx.into_pyobject(py)?.into_any(),
            numpy::ndarray::Array1::from(b.hy.clone()).into_pyarray(py).into_any(),
            b.tag.into_pyobject(py)?.into_any(),
        ])?)?;
    }
    PyTuple::new(py, [strips_py(py, &rails)?.into_any(), bl.into_any(), strips_py(py, &coll)?.into_any()])
}

/// The bed line (n,) of a 3D open tram part over the ground zg at its points (open_bed).
#[pyfunction]
pub fn tram_open_bed<'py>(py: Python<'py>, zg: PyReadonlyArray1<'py, f64>) -> Bound<'py, numpy::PyArray1<f64>> {
    numpy::ndarray::Array1::from(open_bed(&zg.as_array().to_vec())).into_pyarray(py)
}

/// The deck paving of a street tram on a road bridge (deck_paving): (P, T, UV, tag).
#[pyfunction]
pub fn tram_deck_paving<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>, uu: PyReadonlyArray2<'py, f64>,
                             t: PyReadonlyArray1<'py, f64>, v: PyReadonlyArray1<'py, f64>, half_g: f64)
                             -> PyResult<Bound<'py, PyTuple>> {
    strips_py(py, &deck_paving(&pts2(&p), &pts2(&uu), &t.as_array().to_vec(), &v.as_array().to_vec(), half_g))
}

/// road_deck_z of points xy (n, 2) over decks given end to end (pts (m, 2), z (m,), off (d + 1,)): z (n,), NaN where none.
#[pyfunction]
pub fn tram_road_deck_z<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>,
                             z: PyReadonlyArray1<'py, f64>, off: PyReadonlyArray1<'py, i64>)
                             -> PyResult<Bound<'py, numpy::PyArray1<f64>>> {
    let (p, dp, dz, o) = (pts2(&xy), pts2(&pts), z.as_array().to_vec(), off.as_array().to_vec());
    if dz.len() != dp.len() || o.is_empty() || o[o.len() - 1] as usize != dp.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_road_deck_z: decks and offsets differ"));
    }
    let decks: Vec<(Vec<P2>, Vec<f64>)> =
        (0..o.len() - 1).map(|k| (dp[o[k] as usize..o[k + 1] as usize].to_vec(), dz[o[k] as usize..o[k + 1] as usize].to_vec())).collect();
    Ok(numpy::ndarray::Array1::from(py.detach(|| road_deck_z(&p, &decks))).into_pyarray(py))
}

/// rides_deck of line (n, 2) over decks' lines end to end (pts (m, 2), off (d + 1,)).
#[pyfunction]
pub fn tram_rides_deck(line: PyReadonlyArray2<'_, f64>, pts: PyReadonlyArray2<'_, f64>, off: PyReadonlyArray1<'_, i64>,
                       share: f64, step: f64) -> PyResult<bool> {
    let (l, dp, o) = (pts2(&line), pts2(&pts), off.as_array().to_vec());
    if o.is_empty() || o[o.len() - 1] as usize != dp.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_rides_deck: decks and offsets differ"));
    }
    let decks: Vec<Vec<P2>> = (0..o.len() - 1).map(|k| dp[o[k] as usize..o[k + 1] as usize].to_vec()).collect();
    Ok(rides_deck(&l, &decks, share, step))
}

/// The mesh's numbers for the glue.
#[pyfunction]
pub fn tram_mesh_consts(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in [("RAIL_HEAD", RAIL_HEAD), ("SLEEPER_EVERY", SLEEPER_EVERY), ("SLEEPER_L", SLEEPER_L),
                   ("DECK_NEAR_M", DECK_NEAR_M)] {
        d.set_item(k, v)?;
    }
    d.set_item("TAG_STEEL", TAG_STEEL)?;
    d.set_item("TAG_COLLIDER", TAG_COLLIDER)?;
    d.set_item("TAG_SLEEPER", TAG_SLEEPER)?;
    Ok(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(n: usize) -> Vec<P2> {
        (0..n).map(|i| [2.0 * i as f64, 0.0]).collect()
    }

    #[test]
    fn a_straight_part_has_two_rails_sleepers_on_the_ground_and_a_collider() {
        let xy = line(21);
        let zg = vec![10.0; 21];
        let f = vec![false; 21];
        let (rails, boxes, coll) = open_part(&xy, 1.0, &zg, &f, &vec![true; 21], &f, (false, false), None, &[]);
        assert_eq!(rails.t.len(), 2 * 3 * 2 * 20, "two rails: a top and two sides each, 2 triangles a segment");
        assert_eq!(boxes.len(), 1);
        assert_eq!(boxes[0].c.len(), 80, "a sleeper every 0.5 m over 40 m");
        let ztop = boxes[0].z1[10];
        assert!((ztop - (10.0 + 0.02 + 0.04)).abs() < 1e-9, "sleepers half sunk on the ground: {ztop}");
        assert!(rails.p.iter().all(|q| q[2] <= 10.0 + 0.02 + 0.12 + 1e-9));
        assert_eq!(coll.t.len(), 2 * 3 * 2 * 20);
        // every rail top triangle faces up
        for (k, tri) in rails.t.iter().enumerate().take(40) {
            let (a, b, c) = (rails.p[tri[0] as usize], rails.p[tri[1] as usize], rails.p[tri[2] as usize]);
            let nz = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
            assert!(nz > 0.0, "{k}");
        }
    }

    #[test]
    fn the_road_deck_height_and_riding() {
        let deck = (vec![[0.0, 2.0], [40.0, 2.0]], vec![10.0, 12.0]);
        let z = road_deck_z(&[[20.0, 0.0], [20.0, 30.0]], &[deck.clone()]);
        assert!((z[0] - 11.0).abs() < 1e-12 && z[1].is_nan());
        assert!(rides_deck(&line(21), &[deck.0.clone()], 0.25, 2.0));
        assert!(!rides_deck(&[[0.0, 50.0], [40.0, 50.0]], &[deck.0], 0.25, 2.0));
    }
}
