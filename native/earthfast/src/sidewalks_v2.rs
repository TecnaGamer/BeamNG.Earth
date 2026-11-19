//! Sidewalks v2 (server sidewalks.V2, earthfast 502; the road revamp's P1d, 2026-11-19). West Coast USA / Italy / ECA
//! measured (the second vanilla study): the kerb's reveal varies along a street (0.08-0.20 m) because the walk keeps
//! its own smooth profile; suburban streets have rolled, mountable kerbs (8-10 cm at 45-63 deg, a 0.25-0.3 m top)
//! that driveways cross anywhere; walks run on across driveways (a lowered kerb and an apron); the verge is part of
//! the mesh. v2 keeps v1's 8 profile points and strips (sidewalks.rs, untouched: v1's tiles stay byte for byte), with
//! per-vertex reveal, rolled share and back drop, ramps of two kinds (0 a crossing: tactile, RAMP_H; 1 a driveway:
//! no tactile, DRIVE_H), corner slabs whose outer points come at their absolute top (the runs' back edges: one height
//! where they meet), and backs that end on the ground they meet (the verge / plaza: no skirt diving under the road,
//! no gap over lower ground).
use super::*;

const R_BATTER: f64 = 0.12;     // rolled kerb: the face's run over its height (with SINK: ~55 deg above the road)
const R_NOSE: f64 = 0.02;
const R_NOSE_W: f64 = 0.17;
const R_CURB_W: f64 = 0.28;     // ... and its top band (vanilla 0.25-0.3 m)
const DRIVE_H: f64 = 0.03;      // m: the kerb at a driveway (lowered, not flush: a crossing's is RAMP_H)
const DRIVE_FLARE: f64 = 0.9;   // m of kerb sloping back up each side of a driveway
const TOE_EMBED: f64 = 0.02;    // m the back's toe sits under the ground it meets
const DROP_MIN: f64 = 0.03;
const DROP_MAX: f64 = 1.5;
// the walk's own profile (sidewalk_reveal): the drawn ground along the curb and BACK_PROBE_M behind it, averaged over
// PROBE_R x PROBE_STEP m each way along the walk (triangle weights) at every vertex: a function of where the vertex
// is (not of the run's extent), so two tiles, a run and its corner piece agree where they meet
const PROBE_R: usize = 6;
const PROBE_STEP: f64 = 1.0;
const BACK_PROBE_M: f64 = 2.0;
const ALPHA: f64 = 0.5;         // share of the ground's rise behind the walk its top follows

/// The kerb shape for rolled share r (0 vertical, 1 rolled): (batter, nose, nose width, top band).
fn kerb(r: f64) -> (f64, f64, f64, f64) {
    let r = if r.is_finite() { r.clamp(0.0, 1.0) } else { 0.0 };
    (BATTER + r * (R_BATTER - BATTER), NOSE + r * (R_NOSE - NOSE), NOSE_W + r * (R_NOSE_W - NOSE_W),
     CURB_W + r * (R_CURB_W - CURB_W))
}

/// v2's profile: v1's points for reveal h, curb height hr, rolled share roll; the back falls drop m to its toe (NaN:
/// v1's top + SINK, to SINK under the road).
fn profile_v2(h: f64, w: f64, hr: f64, roll: f64, drop: f64) -> [[f64; 2]; 8] {
    let (bt, no, nw, cw) = kerb(roll);
    let f = if h > 0.0 { hr / h } else { 0.0 };
    let rr = (cw + NARROW_EDGE_M).max(RAMP_RUN.min(w - NARROW_EDGE_M));
    let zr = h + rr * CROSS_SLOPE;
    let xp = cw + PAD_W.min((rr - cw) / 2.0);
    let zp = hr + (zr - hr) * (xp - cw) / (rr - cw);
    let top = h + w * CROSS_SLOPE;
    let back = if drop.is_finite() { [w + BACK_RUN * drop, top - drop] } else { [w + BACK_RUN * (top + SINK), -SINK] };
    [[0.0, -SINK], [bt * f, hr - no * f], [nw * f, hr], [cw, hr], [xp, zp], [rr, zr], [w, top], back]
}

fn ramp_shape(kind: i64) -> (f64, f64) {
    if kind == 1 { (DRIVE_H, DRIVE_FLARE) } else { (RAMP_H, RAMP_FLARE) }
}

/// refine with each ramp's own flare.
fn refine_v2(s: &[f64], ramps: &[(f64, f64, i64)], step: f64, margin: f64) -> Vec<f64> {
    let last = s[s.len() - 1];
    let mut all: Vec<f64> = s.to_vec();
    for &(a, b, kind) in ramps {
        let fl = ramp_shape(kind).1;
        let start = (a - fl - margin).max(0.0);
        let stop = last.min(b + fl + margin);
        // (a driveway's kerb is linear between its four breakpoints: those alone, no 0.5 m rows along a street that
        // has one every 15 m)
        let n = if kind == 1 { 0.0 } else { ((stop - start) / step).ceil() };
        if n > 0.0 && n < 1e6 {
            for i in 0..n as usize {
                all.push(start + i as f64 * step);
            }
        }
        for v in [a, b, a - fl, b + fl] {
            if 0.0 < v && v < last {
                all.push(v);
            }
        }
    }
    all.retain(|v| v.is_finite());
    all.sort_by(|x, y| x.partial_cmp(y).unwrap());
    all.dedup();
    all
}

/// (curb heights, lowered by a crossing) at s for reveals hv.
fn curb_heights_v2(s: &[f64], ramps: &[(f64, f64, i64)], hv: &[f64]) -> (Vec<f64>, Vec<bool>) {
    let mut hr = hv.to_vec();
    let mut tac = vec![false; s.len()];
    for &(s0, s1, kind) in ramps {
        let (low, fl) = ramp_shape(kind);
        for (i, &v) in s.iter().enumerate() {
            let low_i = low.min(hv[i]);
            let d = (s0 - v).max(v - s1);
            let f = (d / fl).clamp(0.0, 1.0);
            let c = low_i + f * (hv[i] - low_i);
            if c < hr[i] {
                hr[i] = c;
            }
            if kind != 1 && c < hv[i] - 1e-6 {
                tac[i] = true;
            }
        }
    }
    (hr, tac)
}

fn resample(s: &[f64], s2: &[f64], v: &[f64]) -> Vec<f64> {
    s2.iter().map(|&x| interp(x, s, v)).collect()
}

impl Mesh {
    #[allow(clippy::too_many_arguments)]
    fn sections_v2(curb: &[[f64; 3]], out: &[[f64; 2]], w: f64, hv: &[f64], hr: &[f64], roll: &[f64], drop: &[f64],
                   origin: [f64; 3]) -> (Vec<Vec<[f64; 3]>>, Vec<Vec<[f64; 2]>>) {
        let mut ring = Vec::with_capacity(curb.len());
        let mut prof = Vec::with_capacity(curb.len());
        for (i, c) in curb.iter().enumerate() {
            let pr = profile_v2(hv[i], w, hr[i], roll[i], drop[i]);
            ring.push(pr.iter().map(|q| [c[0] + out[i][0] * q[0] - origin[0], c[1] + out[i][1] * q[0] - origin[1],
                                         c[2] + q[1] - origin[2]]).collect());
            prof.push(pr.to_vec());
        }
        (ring, prof)
    }

    /// A v2 run: per-vertex reveal hv, back drop, rolled share; ramps (s0, s1, kind).
    #[allow(clippy::too_many_arguments)]
    pub(super) fn run_v2(&mut self, curb0: &[[f64; 3]], out0: &[[f64; 2]], w: f64, hv0: &[f64], drop0: &[f64], roll0: &[f64],
              caps: (bool, bool), ramps: &[(f64, f64, i64)], slab: i64, lean: bool, origin: [f64; 3]) {
        if curb0.len() < 2 {
            return;
        }
        let mut curb = curb0.to_vec();
        let mut out = out0.to_vec();
        let (mut hv, mut drop, mut roll) = (hv0.to_vec(), drop0.to_vec(), roll0.to_vec());
        let mut s = arclen(&curb);
        if !ramps.is_empty() {
            let s2 = refine_v2(&s, ramps, 0.5, 1.0);
            let col = |c: usize| -> Vec<f64> { curb.iter().map(|p| p[c]).collect() };
            let (cx, cy, cz) = (col(0), col(1), col(2));
            let ox: Vec<f64> = out.iter().map(|p| p[0]).collect();
            let oy: Vec<f64> = out.iter().map(|p| p[1]).collect();
            curb = s2.iter().map(|&v| [interp(v, &s, &cx), interp(v, &s, &cy), interp(v, &s, &cz)]).collect();
            out = s2.iter().map(|&v| [interp(v, &s, &ox), interp(v, &s, &oy)]).collect();
            hv = resample(&s, &s2, &hv);
            drop = resample(&s, &s2, &drop);
            roll = resample(&s, &s2, &roll);
            s = s2;
        }
        let (mut hr, mut tac_low) = curb_heights_v2(&s, ramps, &hv);
        if lean {
            let pts: Vec<Vec<f64>> = (0..curb.len()).map(|i| vec![curb[i][0], curb[i][1], curb[i][2], hr[i], hv[i],
                if drop[i].is_finite() { drop[i] } else { 0.0 }, 0.1 * roll[i]]).collect();
            let keep = simplify(&pts, LEAN_TOL_M);
            curb = keep.iter().map(|&i| curb[i]).collect();
            out = keep.iter().map(|&i| out[i]).collect();
            s = keep.iter().map(|&i| s[i]).collect();
            hr = keep.iter().map(|&i| hr[i]).collect();
            hv = keep.iter().map(|&i| hv[i]).collect();
            drop = keep.iter().map(|&i| drop[i]).collect();
            roll = keep.iter().map(|&i| roll[i]).collect();
            tac_low = keep.iter().map(|&i| tac_low[i]).collect();
        }
        let k = curb.len();
        let (mut ring, mut prof) = Mesh::sections_v2(&curb, &out, w, &hv, &hr, &roll, &drop, origin);
        if lean {
            ring = ring.iter().map(|r| LEAN.iter().map(|&j| r[j]).collect()).collect();
            prof = prof.iter().map(|r| LEAN.iter().map(|&j| r[j]).collect()).collect();
            let ls: Vec<(usize, usize, i64, f64, f64)> = (0..LEAN.len() - 1).map(|i| (i, i + 1, 0, 0.0, 0.0)).collect();
            self.strips(&ring, &prof, &out, &s, &ls, None, 1);
        } else {
            let tac: Option<Vec<bool>> = if tac_low.iter().any(|&v| v) {
                Some((0..k - 1).map(|i| tac_low[i] || tac_low[i + 1]).collect())
            } else { None };
            self.strips(&ring, &prof, &out, &s, &strips(), tac.as_deref(), slab);
        }
        for (end, sgn, cap) in [(0usize, -1.0f64, caps.0), (k - 1, 1.0, caps.1)] {
            if !cap {
                continue;
            }
            let p: Vec<[f64; 3]> = ring[end].clone();
            let pr = &prof[end];
            let h = hv[end];
            let uv: Vec<[f64; 2]> = pr.iter().map(|q| [q[0] / CURB_REPEAT_M,
                v_band_low() + (q[1] + SINK) / (h + SINK + 0.05) * (v_light_low() - v_band_low())]).collect();
            let local = p.clone();
            let base = self.add(p, uv);
            let a = curb[(end + 1).min(k - 1)];
            let b = curb[if end >= 1 { end - 1 } else { 0 }];
            let (tx, ty) = (a[0] - b[0], a[1] - b[1]);
            let l = tx.dhypot(ty).max(1e-9);
            let want = [tx / l * sgn, ty / l * sgn, 0.0];
            for j in 1..pr.len() as i64 - 1 {
                let t = orient([0, j, j + 1], &local, want);
                self.t.push([t[0] + base, t[1] + base, t[2] + base]);
                self.m.push(0);
            }
        }
    }

    /// A v2 corner: outer points at their absolute slab top, otoe the ground under each (NaN: v1's drop).
    #[allow(clippy::too_many_arguments)]
    pub(super) fn corner_v2(&mut self, arc0: &[[f64; 3]], inward0: &[[f64; 2]], outer: &[[f64; 3]], otoe: &[f64], hv0: &[f64],
                 roll0: &[f64], ramps: &[(f64, f64, i64)], lean: bool, origin: [f64; 3]) {
        if arc0.is_empty() {
            return;
        }
        let mut arc = arc0.to_vec();
        let mut inward = inward0.to_vec();
        let (mut hv, mut roll) = (hv0.to_vec(), roll0.to_vec());
        let mut s = arclen(&arc);
        if !ramps.is_empty() {
            let s2 = refine_v2(&s, ramps, 0.5, 1.0);
            let col = |c: usize| -> Vec<f64> { arc.iter().map(|p| p[c]).collect() };
            let (cx, cy, cz) = (col(0), col(1), col(2));
            let ix: Vec<f64> = inward.iter().map(|p| p[0]).collect();
            let iy: Vec<f64> = inward.iter().map(|p| p[1]).collect();
            arc = s2.iter().map(|&v| [interp(v, &s, &cx), interp(v, &s, &cy), interp(v, &s, &cz)]).collect();
            inward = s2.iter().map(|&v| {
                let (x, y) = (interp(v, &s, &ix), interp(v, &s, &iy));
                let n = x.dhypot(y).max(1e-9);
                [x / n, y / n]
            }).collect();
            hv = resample(&s, &s2, &hv);
            roll = resample(&s, &s2, &roll);
            s = s2;
        }
        let (mut hr, _) = curb_heights_v2(&s, ramps, &hv);
        if lean {
            let pts: Vec<Vec<f64>> = (0..arc.len()).map(|i| vec![arc[i][0], arc[i][1], arc[i][2], hr[i], hv[i],
                                                               0.1 * roll[i]]).collect();
            let keep = simplify(&pts, LEAN_TOL_M);
            arc = keep.iter().map(|&i| arc[i]).collect();
            inward = keep.iter().map(|&i| inward[i]).collect();
            s = keep.iter().map(|&i| s[i]).collect();
            hr = keep.iter().map(|&i| hr[i]).collect();
            hv = keep.iter().map(|&i| hv[i]).collect();
            roll = keep.iter().map(|&i| roll[i]).collect();
        }
        let k = arc.len();
        let nan = vec![f64::NAN; k];
        let (ring, prof) = Mesh::sections_v2(&arc, &inward, 2.5, &hv, &hr, &roll, &nan, origin);
        if lean {
            self.strips(&ring, &prof, &inward, &s, &[(0, 3, 0, 0.0, 0.0)], None, 1);
        } else {
            let cs: Vec<(usize, usize, i64, f64, f64)> = strips()[..3].to_vec();
            self.strips(&ring, &prof, &inward, &s, &cs, None, 1);
        }
        let mut poly: Vec<[f64; 3]> = ring.iter().map(|r| r[3]).collect();
        let out: Vec<[f64; 3]> = outer.iter().map(|o| [o[0] - origin[0], o[1] - origin[1], o[2] - origin[2]]).collect();
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
        let mean_h = if hv.is_empty() { CURB_H } else { hv.iter().sum::<f64>() / hv.len() as f64 };
        let fallback = mean_h + SINK + 2.5 * CROSS_SLOPE;
        let drop_at = |j: usize| -> f64 {
            let g = otoe.get(j).copied().unwrap_or(f64::NAN);
            if g.is_finite() {
                (outer[j][2] - (g - TOE_EMBED)).clamp(DROP_MIN, DROP_MAX)
            } else {
                fallback
            }
        };
        for (j, w2) in out.windows(2).enumerate() {
            let (a, b) = (w2[0], w2[1]);
            let t = [b[0] - a[0], b[1] - a[1]];
            let l = t[0].dhypot(t[1]).max(1e-9);
            let mut nrm = [t[1] / l, -t[0] / l];
            let mid = [0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])];
            if nrm[0] * (mid[0] - c[0]) + nrm[1] * (mid[1] - c[1]) < 0.0 {
                nrm = [-nrm[0], -nrm[1]];
            }
            let (da, db) = (drop_at(j), drop_at(j + 1));
            let p = vec![a, b, [b[0] + nrm[0] * BACK_RUN * db, b[1] + nrm[1] * BACK_RUN * db, b[2] - db],
                         [a[0] + nrm[0] * BACK_RUN * da, a[1] + nrm[1] * BACK_RUN * da, a[2] - da]];
            let len = (b[0] - a[0]).dhypot(b[1] - a[1]) / CURB_REPEAT_M;
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

/// sidewalks.Builder.arrays under sidewalks v2: sidewalk_mesh's pieces with ramps (s0, s1, kind) rows and per-point
/// hv (the reveal), drop (the back's fall to its toe, NaN v1's), roll (the rolled share) on pts' rows, and otoe (the
/// ground under each outer point, NaN v1's drop) on outer's rows; a corner's outer points at their absolute top.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn sidewalk_mesh2<'py>(py: Python<'py>, kind: PyReadonlyArray1<'py, i64>, pts: PyReadonlyArray2<'py, f64>,
                           dirs: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                           outer: PyReadonlyArray2<'py, f64>, ooff: PyReadonlyArray1<'py, i64>,
                           ramps: PyReadonlyArray2<'py, f64>, roff: PyReadonlyArray1<'py, i64>,
                           w: PyReadonlyArray1<'py, f64>, caps: PyReadonlyArray2<'py, bool>,
                           slab: PyReadonlyArray1<'py, i64>, hv: PyReadonlyArray1<'py, f64>,
                           drop: PyReadonlyArray1<'py, f64>, roll: PyReadonlyArray1<'py, f64>,
                           otoe: PyReadonlyArray1<'py, f64>, origin: (f64, f64, f64), collision: bool)
                           -> PyResult<(MeshOut<'py>, MeshOut<'py>)> {
    let (kind, pts, dirs, off) = (kind.as_array(), pts.as_array(), dirs.as_array(), off.as_array());
    let (outer, ooff, ramps, roff) = (outer.as_array(), ooff.as_array(), ramps.as_array(), roff.as_array());
    let (w, caps, slab) = (w.as_array(), caps.as_array(), slab.as_array());
    let (hv, drop, roll, otoe) = (hv.as_array(), drop.as_array(), roll.as_array(), otoe.as_array());
    let n = kind.len();
    let np_ = pts.nrows();
    let ok_off = |o: &numpy::ndarray::ArrayView1<'_, i64>, total: usize| -> bool {
        o.len() == n + 1 && o[0] == 0 && o[n] >= 0 && o[n] as usize == total
            && (0..n).all(|i| o[i] >= 0 && o[i] <= o[i + 1])
    };
    if !ok_off(&off, np_) || !ok_off(&ooff, outer.nrows()) || !ok_off(&roff, ramps.nrows()) || w.len() != n
        || caps.nrows() != n || caps.ncols() != 2 || slab.len() != n || dirs.nrows() != np_
        || (np_ > 0 && (dirs.ncols() < 2 || pts.ncols() < 2)) || (outer.nrows() > 0 && outer.ncols() < 3)
        || (ramps.nrows() > 0 && ramps.ncols() < 3) || hv.len() != np_ || drop.len() != np_ || roll.len() != np_
        || otoe.len() != outer.nrows() {
        return Err(PyValueError::new_err("sidewalk_mesh2: inconsistent piece arrays"));
    }
    let o = [origin.0, origin.1, origin.2];
    let (mut full, mut lean) = (Mesh::default(), Mesh::default());
    for i in 0..n {
        let (a, b) = (off[i] as usize, off[i + 1] as usize);
        let p = pts3(&pts, a..b);
        let d = pts2(&dirs, a..b);
        let rr: Vec<(f64, f64, i64)> = (roff[i] as usize..roff[i + 1] as usize)
            .map(|j| (ramps[[j, 0]], ramps[[j, 1]], ramps[[j, 2]] as i64)).collect();
        let hv_i: Vec<f64> = (a..b).map(|j| hv[j]).collect();
        let roll_i: Vec<f64> = (a..b).map(|j| roll[j]).collect();
        if kind[i] == 0 {
            let drop_i: Vec<f64> = (a..b).map(|j| drop[j]).collect();
            let c = (caps[[i, 0]], caps[[i, 1]]);
            if collision {
                lean.run_v2(&p, &d, w[i], &hv_i, &drop_i, &roll_i, c, &rr, 1, true, o);
            }
            full.run_v2(&p, &d, w[i], &hv_i, &drop_i, &roll_i, c, &rr, slab[i], false, o);
        } else {
            let (oa, ob) = (ooff[i] as usize, ooff[i + 1] as usize);
            let ou = pts3(&outer, oa..ob);
            let ot: Vec<f64> = (oa..ob).map(|j| otoe[j]).collect();
            if collision {
                lean.corner_v2(&p, &d, &ou, &ot, &hv_i, &roll_i, &rr, true, o);
            }
            full.corner_v2(&p, &d, &ou, &ot, &hv_i, &roll_i, &rr, false, o);
        }
    }
    Ok((mesh_out(py, full), mesh_out(py, lean)))
}

/// sidewalks v2: the ground samples of the walk's own profile (sidewalk_reveal) at every vertex xy (k, 2) with its
/// outward direction nrm (k, 2; any length): per vertex 2 S + 1 points (S = 2 PROBE_R + 1): along the curb line,
/// along the line BACK_PROBE_M behind it, then the toe at toe_d m out (k,). Pure.
#[pyfunction]
pub fn sidewalk_probes<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, nrm: PyReadonlyArray2<'py, f64>,
                            toe_d: PyReadonlyArray1<'py, f64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (xy, nrm, toe_d) = (xy.as_array(), nrm.as_array(), toe_d.as_array());
    let k = xy.nrows();
    if nrm.nrows() != k || toe_d.len() != k || (k > 0 && (xy.ncols() < 2 || nrm.ncols() < 2)) {
        return Err(PyValueError::new_err("sidewalk_probes: mismatched arrays"));
    }
    let s = 2 * PROBE_R + 1;
    let per = 2 * s + 1;
    let mut out: Vec<f64> = Vec::with_capacity(k * per * 2);
    for i in 0..k {
        let (px, py_) = (xy[[i, 0]], xy[[i, 1]]);
        let l = nrm[[i, 0]].dhypot(nrm[[i, 1]]);
        let (nx, ny) = if l > 1e-12 { (nrm[[i, 0]] / l, nrm[[i, 1]] / l) } else { (0.0, 0.0) };
        let (tx, ty) = (-ny, nx);
        for back in [0.0, BACK_PROBE_M] {
            for j in 0..s {
                let d = (j as f64 - PROBE_R as f64) * PROBE_STEP;
                out.push(px + tx * d + nx * back);
                out.push(py_ + ty * d + ny * back);
            }
        }
        let td = toe_d[i];
        out.push(px + nx * td);
        out.push(py_ + ny * td);
    }
    Ok(numpy::ndarray::Array2::from_shape_vec((k * per, 2), out).unwrap().into_pyarray(py))
}

/// sidewalks v2: the walk's reveal (its top over the drawn ground at the curb line) and its back's drop at every
/// vertex, from sidewalk_probes' ground heights z (k * (2 S + 1),; NaN: none there). The top follows the curb line's
/// ground averaged along the walk plus ALPHA of the averaged rise behind it, at h0 (the nominal reveal); the reveal is
/// that top over the vertex's own ground, within [lo, hi] (h0 where lo > hi). drop: from the slab's outer top
/// (reveal + w x CROSS_SLOPE) to TOE_EMBED under the toe's ground, within [DROP_MIN, DROP_MAX] (NaN without w or a
/// toe). Pure, in a fixed order.
#[pyfunction]
pub fn sidewalk_reveal<'py>(py: Python<'py>, z: PyReadonlyArray1<'py, f64>, h0: PyReadonlyArray1<'py, f64>,
                            lo: PyReadonlyArray1<'py, f64>, hi: PyReadonlyArray1<'py, f64>,
                            w: PyReadonlyArray1<'py, f64>)
                            -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (z, h0, lo, hi, w) = (z.as_array(), h0.as_array(), lo.as_array(), hi.as_array(), w.as_array());
    let k = h0.len();
    let s = 2 * PROBE_R + 1;
    let per = 2 * s + 1;
    if z.len() != k * per || lo.len() != k || hi.len() != k || w.len() != k {
        return Err(PyValueError::new_err("sidewalk_reveal: mismatched arrays"));
    }
    let mut hv = Vec::with_capacity(k);
    let mut dr = Vec::with_capacity(k);
    for i in 0..k {
        let base = i * per;
        let avg = |off: usize| -> f64 {
            let (mut sw, mut sz) = (0.0f64, 0.0f64);
            for j in 0..s {
                let v = z[base + off + j];
                if v.is_finite() {
                    let wt = (PROBE_R + 1) as f64 - (j as f64 - PROBE_R as f64).abs();
                    sw += wt;
                    sz += wt * v;
                }
            }
            if sw > 0.0 { sz / sw } else { f64::NAN }
        };
        let g0 = z[base + PROBE_R];
        let (l, h, n0) = (lo[i], hi[i], h0[i]);
        let r = if !g0.is_finite() || l.is_nan() || h.is_nan() || l > h {
            n0
        } else {
            let gc = avg(0);
            let gb = avg(s);
            let gc = if gc.is_finite() { gc } else { g0 };
            let rise = if gb.is_finite() { gb - gc } else { 0.0 };
            (gc + n0 + ALPHA * rise - g0).clamp(l, h)
        };
        hv.push(r);
        let toe = z[base + 2 * s];
        let d = if g0.is_finite() && toe.is_finite() && w[i].is_finite() {
            (r + w[i] * CROSS_SLOPE - (toe - g0 - TOE_EMBED)).clamp(DROP_MIN, DROP_MAX)
        } else {
            f64::NAN
        };
        dr.push(d);
    }
    Ok((hv.into_pyarray(py), dr.into_pyarray(py)))
}

/// sidewalks v2: where lines cross a walk's polyline run (k, 2): [(s0, s1)] arc-length ranges along the run, each
/// crossing's s +- its line's half width over the sine of the crossing angle (at most 2 x the half width), merged
/// where they overlap, sorted. lines: their points (m, 2), loff (L + 1) offsets, half (L,). Pure.
#[pyfunction]
pub fn walk_crossings(run: PyReadonlyArray2<'_, f64>, lines: PyReadonlyArray2<'_, f64>,
                      loff: PyReadonlyArray1<'_, i64>, half: PyReadonlyArray1<'_, f64>) -> PyResult<Vec<(f64, f64)>> {
    let (run, lines, loff, half) = (run.as_array(), lines.as_array(), loff.as_array(), half.as_array());
    let nl = half.len();
    if loff.len() != nl + 1 || loff[0] != 0 || loff[nl] < 0 || loff[nl] as usize != lines.nrows()
        || (0..nl).any(|i| loff[i] > loff[i + 1]) || (run.nrows() > 0 && run.ncols() < 2)
        || (lines.nrows() > 0 && lines.ncols() < 2) {
        return Err(PyValueError::new_err("walk_crossings: mismatched arrays"));
    }
    let k = run.nrows();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (run[[i, 0]] - run[[i - 1, 0]]).dhypot(run[[i, 1]] - run[[i - 1, 1]]);
    }
    let mut got: Vec<(f64, f64)> = Vec::new();
    for li in 0..nl {
        let (a, b) = (loff[li] as usize, loff[li + 1] as usize);
        let hw = half[li];
        if !hw.is_finite() || hw <= 0.0 {
            continue;
        }
        for j in a..b.saturating_sub(1) {
            let (qx, qy) = (lines[[j, 0]], lines[[j, 1]]);
            let (ex, ey) = (lines[[j + 1, 0]] - qx, lines[[j + 1, 1]] - qy);
            let el = ex.dhypot(ey);
            if el.is_nan() || el <= 1e-9 {
                continue;
            }
            for i in 0..k.saturating_sub(1) {
                let (px, py_) = (run[[i, 0]], run[[i, 1]]);
                let (dx, dy) = (run[[i + 1, 0]] - px, run[[i + 1, 1]] - py_);
                let dl = dx.dhypot(dy);
                if dl.is_nan() || dl <= 1e-9 {
                    continue;
                }
                let den = dx * ey - dy * ex;
                if den.is_nan() || den.abs() < 1e-12 {
                    continue;
                }
                let (rx, ry) = (qx - px, qy - py_);
                let t = (rx * ey - ry * ex) / den;
                let u = (rx * dy - ry * dx) / den;
                if !(0.0..=1.0).contains(&t) || !(0.0..=1.0).contains(&u) {
                    continue;
                }
                let sin = (den / (dl * el)).abs();
                let reach = (hw / sin.max(0.5)).min(2.0 * hw);
                let at = s[i] + t * dl;
                got.push((at - reach, at + reach));
            }
        }
    }
    got.sort_by(|x, y| x.0.total_cmp(&y.0).then(x.1.total_cmp(&y.1)));
    let mut out: Vec<(f64, f64)> = Vec::new();
    for (a, b) in got {
        if let Some(last) = out.last_mut() {
            if a <= last.1 {
                if b > last.1 {
                    last.1 = b;
                }
                continue;
            }
        }
        out.push((a, b));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vertical_matches_v1_profile() {
        // roll 0, no drop given: v2's profile is v1's
        for (h, w, hr) in [(0.15, 2.5, 0.15), (0.15, 2.0, 0.01), (0.12, 0.5, 0.12)] {
            assert_eq!(profile(h, w, hr), profile_v2(h, w, hr, 0.0, f64::NAN));
        }
    }

    #[test]
    fn rolled_kerb_is_mountable() {
        let p = profile_v2(0.09, 2.5, 0.09, 1.0, f64::NAN);
        // 45-63 deg: a slope between tan 45 = 1 and tan 63 = 1.963
        let slope = (p[1][1] - p[0][1]) / (p[1][0] - p[0][0]);
        assert!(slope > 1.0 && slope < 1.963, "{slope}");
        assert!((p[3][0] - 0.28).abs() < 1e-12);
    }

    #[test]
    fn driveway_lowers_without_tactile() {
        let s: Vec<f64> = (0..21).map(|i| i as f64 * 0.5).collect();
        let hv = vec![0.15; s.len()];
        let (hr, tac) = curb_heights_v2(&s, &[(4.0, 6.0, 1)], &hv);
        assert!(tac.iter().all(|&t| !t));
        assert!((hr[10] - DRIVE_H).abs() < 1e-12);
        assert!((hr[0] - 0.15).abs() < 1e-12);
        let (_, tac2) = curb_heights_v2(&s, &[(4.0, 6.0, 0)], &hv);
        assert!(tac2[10]);
    }

    #[test]
    fn a_v2_run_has_v1_counts() {
        let mut m = Mesh::default();
        let curb: Vec<[f64; 3]> = (0..5).map(|i| [i as f64 * 2.0, 0.0, 1.0]).collect();
        let out = vec![[0.0, 1.0]; 5];
        m.run_v2(&curb, &out, 2.5, &[0.15; 5], &[f64::NAN; 5], &[0.0; 5], (true, true), &[], 1, false, [0.0; 3]);
        assert_eq!(m.p.len(), 7 * 2 * 5 + 2 * 8);
        assert_eq!(m.t.len(), 7 * 2 * 4 + 2 * 6);
    }
}
