//! Road revamp v2 phase P3b (earthfast 521, BNE_ROADSHAPE=profile): the joint profile's approach ground into the graded
//! ground (server/grading.py's second stage, grading.PROFILE_PINS, _Planner.apply).
//!
//! profile_pin_field: per point, the change the joint solve made to the approach ground it lies on (roadshape._built's
//! pins: an approach member's knots, the solve's change dz there, its fade w along it (1 near its abutment, 0 at its
//! reach), its height today h, its road's half width), added to the first stage's graded height there:
//! - each member weighs a point by its distance d to the member's line (the foot clamped to the line: past its first
//!   knot, under the deck end, the distance grows from that knot): 1 within the half width, a smoothstep to 0 at
//!   half + taper (the plans' transition back to the ground, grading.T_MAX), times a height gate: 1 while the point's
//!   graded height is within gate0 of the member's height today at the foot, a smoothstep to 0 at gate1 (a road at
//!   another level beside or over the approach, a ramp's embankment, keeps its own ground), times its fade at the foot
//!   (another chain's approach faded out at this abutment gives way to this one's);
//! - the change at a point: (sum a^32 dz / sum a^32) x max w over the members, a a member's share: w with its distance
//!   weight a smoothstep from 1 on its line to 0 at half + taper (one member: w dz; twins side by side: their mean
//!   midway; on a member's line another whose half width covers it gives way; times its share along it, 1 at its
//!   abutment and less away from it: a ground road between two chains is each one's near its own abutment; 523),
//!   dz linear between the member's knots at the foot; nearest segment per member
//!   (ties: the lower segment). On the member's line, away from other members, the graded height today plus dz: the
//!   solve's height exactly.
//! Pure, basic IEEE operations and sqrt only (no FMA, no libm), fixed loop order: the same on every platform.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub const PIN_PARAMS: usize = 3;

pub struct Pin {
    pub xy: Vec<[f64; 2]>,
    pub dz: Vec<f64>,
    pub h: Vec<f64>,
    pub w: Vec<f64>,
    pub sh: Vec<f64>,       // (its share along it: 1 at its abutment, less away from it, 523)
    pub half: f64,
    pub lo: [f64; 2],
    pub hi: [f64; 2],
}

pub fn pin(xy: Vec<[f64; 2]>, dz: Vec<f64>, h: Vec<f64>, w: Vec<f64>, sh: Vec<f64>, half: f64) -> Pin {
    let mut lo = [f64::INFINITY; 2];
    let mut hi = [f64::NEG_INFINITY; 2];
    for p in &xy {
        for d in 0..2 {
            if p[d] < lo[d] {
                lo[d] = p[d];
            }
            if p[d] > hi[d] {
                hi[d] = p[d];
            }
        }
    }
    Pin { xy, dz, h, w, sh, half, lo, hi }
}

fn smooth(f: f64) -> f64 {
    let f = f.max(0.0).min(1.0);
    f * f * (3.0 - 2.0 * f)
}

/// The change per point (xy, its first-stage graded height z) from the pins. prm: taper (m), gate0, gate1 (m).
pub fn field_core(pts: &[[f64; 2]], z: &[f64], pins: &[Pin], prm: &[f64]) -> Vec<f64> {
    let (taper, gate0, gate1) = (prm[0], prm[1], prm[2]);
    let mut out = vec![0.0f64; pts.len()];
    for (v, p) in pts.iter().enumerate() {
        let (mut sw2, mut swdz, mut wmax) = (0.0f64, 0.0f64, 0.0f64);
        for q in pins {
            let reach = q.half + taper;
            if p[0] < q.lo[0] - reach || p[0] > q.hi[0] + reach || p[1] < q.lo[1] - reach || p[1] > q.hi[1] + reach {
                continue;
            }
            let m = q.xy.len();
            let (mut best, mut bdz, mut bh, mut bw, mut bs) = (f64::INFINITY, 0.0f64, 0.0f64, 0.0f64, 0.0f64);
            for j in 0..m.saturating_sub(1) {
                let (s0, s1) = (q.xy[j], q.xy[j + 1]);
                let (ex, ey) = (s1[0] - s0[0], s1[1] - s0[1]);
                let l2 = ex * ex + ey * ey;
                let t = if l2 > 1e-12 { (((p[0] - s0[0]) * ex + (p[1] - s0[1]) * ey) / l2).max(0.0).min(1.0) } else { 0.0 };
                let (fx, fy) = (s0[0] + t * ex - p[0], s0[1] + t * ey - p[1]);
                let d = (fx * fx + fy * fy).sqrt();
                if d < best {
                    best = d;
                    bdz = q.dz[j] + t * (q.dz[j + 1] - q.dz[j]);
                    bh = q.h[j] + t * (q.h[j + 1] - q.h[j]);
                    bw = q.w[j] + t * (q.w[j + 1] - q.w[j]);
                    bs = q.sh[j] + t * (q.sh[j + 1] - q.sh[j]);
                }
            }
            if !(best < reach) {
                continue;
            }
            let wl = if best <= q.half { 1.0 } else { smooth(1.0 - (best - q.half) / taper) };
            let dh = (z[v] - bh).abs();
            let wg = if dh <= gate0 { 1.0 } else { smooth(1.0 - (dh - gate0) / (gate1 - gate0)) };
            let w = wl * wg * bw.max(0.0).min(1.0);
            if w > 0.0 {
                // (w^8: the nearer road all but alone, a ramp 10 m off with its own change no longer pulls this
                // one's (Ottawa t4: 0.4 m); twins side by side, equal, still their mean. The share by the distance
                // from the line itself, not flat over the half width (P3c, earthfast 523): on its own line a member
                // all but alone though another's wide deck covers it (Nordschleife A: a 14 m and an 8 m deck 7 m
                // apart, each line given the mean of both changes, 0.2 m off))
                // (and by its share along it: one ground road between two chains' abutments, an approach of each,
                // is each one's near its own abutment: Ottawa t4, the mean of both pulled 0.3 m off at 25 m)
                let wa = smooth(1.0 - best / reach) * wg * bw.max(0.0).min(1.0) * bs.max(0.0).min(1.0);
                let w2 = wa * wa;
                let w8 = (w2 * w2) * (w2 * w2);
                // (a^32: a wide deck's approach 5 m off, a = 0.87, gave way too little at the I-95: 1.5 m)
                let w8 = (w8 * w8) * (w8 * w8);
                sw2 += w8;
                swdz += w8 * bdz;
                if w > wmax {
                    wmax = w;
                }
            }
        }
        if sw2 > 0.0 {
            out[v] = swdz / sw2 * wmax;
        }
    }
    out
}

fn check_off(off: &[i64], n: usize) -> bool {
    !off.is_empty() && off[0] == 0 && off[off.len() - 1] as usize == n && off.windows(2).all(|w| w[0] <= w[1])
}

/// profile_pin_field(points (n, 2), their graded heights (n,), pin knots (m, 2), pin offsets, pin dz (m,), pin heights
/// today (m,), pin fades (m,), pin shares (m,), pin half widths, params) -> the change per point (n,): see the module's notes.
#[pyfunction]
pub fn profile_pin_field<'py>(py: Python<'py>, pxy: PyReadonlyArray2<'py, f64>, pz: PyReadonlyArray1<'py, f64>,
                              qxy: PyReadonlyArray2<'py, f64>, qoff: Vec<i64>, qdz: PyReadonlyArray1<'py, f64>,
                              qh: PyReadonlyArray1<'py, f64>, qw: PyReadonlyArray1<'py, f64>, qs: PyReadonlyArray1<'py, f64>,
                              qhalf: Vec<f64>,
                              params: Vec<f64>)
                              -> PyResult<Vec<f64>> {
    let (pxy, pz, qxy, qdz, qh, qw, qs) = (pxy.as_array(), pz.as_array(), qxy.as_array(), qdz.as_array(),
                                           qh.as_array(), qw.as_array(), qs.as_array());
    if pxy.ncols() != 2 || qxy.ncols() != 2 || pz.len() != pxy.nrows() || !check_off(&qoff, qxy.nrows())
        || qhalf.len() + 1 != qoff.len() || qdz.len() != qxy.nrows() || qh.len() != qxy.nrows() || qw.len() != qxy.nrows()
        || qs.len() != qxy.nrows()
        || params.len() != PIN_PARAMS || !(params[0] > 0.0) || !(params[2] > params[1]) {
        return Err(PyValueError::new_err("profile_pin_field: bad shapes"));
    }
    let pts: Vec<[f64; 2]> = (0..pxy.nrows()).map(|k| [pxy[[k, 0]], pxy[[k, 1]]]).collect();
    let z: Vec<f64> = pz.iter().copied().collect();
    let pins: Vec<Pin> = (0..qhalf.len()).map(|i| {
        let r = qoff[i] as usize..qoff[i + 1] as usize;
        pin(r.clone().map(|k| [qxy[[k, 0]], qxy[[k, 1]]]).collect(), r.clone().map(|k| qdz[k]).collect(),
            r.clone().map(|k| qh[k]).collect(), r.clone().map(|k| qw[k]).collect(), r.map(|k| qs[k]).collect(), qhalf[i])
    }).collect();
    Ok(py.detach(|| field_core(&pts, &z, &pins, &params)))
}

#[cfg(test)]
mod tests {
    use super::*;

    const PRM: [f64; 3] = [12.0, 1.0, 3.0];

    #[test]
    fn on_line_exact_tapered_beside_gated_above() {
        let q = pin(vec![[0.0, 0.0], [100.0, 0.0]], vec![2.0, 2.0], vec![10.0, 10.0], vec![1.0, 0.0], vec![1.0, 1.0], 5.0);
        let pts = [[50.0, 0.0], [50.0, 11.0], [50.0, 30.0], [50.0, 2.0], [-6.0, 0.0]];
        let z = [10.0, 10.0, 10.0, 17.0, 10.0];
        let got = field_core(&pts, &z, &[q], &PRM);
        assert!((got[0] - 1.0).abs() < 1e-12);         // (on the line: its dz)
        assert!(got[1] > 0.0 && got[1] < 1.0);         // (in the taper)
        assert_eq!(got[2], 0.0);                       // (past it)
        assert_eq!(got[3], 0.0);                       // (a road 7 m over the approach: its own)
        assert!(got[4] > 0.0 && got[4] < 2.0);         // (behind the first knot, under the deck end: tapered)
    }

    #[test]
    fn twins_mean() {
        let a = pin(vec![[0.0, 0.0], [100.0, 0.0]], vec![1.0, 1.0], vec![0.0, 0.0], vec![1.0, 1.0], vec![1.0, 1.0], 5.0);
        let b = pin(vec![[0.0, 4.0], [100.0, 4.0]], vec![3.0, 3.0], vec![0.0, 0.0], vec![1.0, 1.0], vec![1.0, 1.0], 5.0);
        let got = field_core(&[[50.0, 2.0]], &[0.0], &[a, b], &PRM);
        assert!((got[0] - 2.0).abs() < 1e-12);
    }

    #[test]
    fn faded_member_gives_way() {
        // (another chain's approach ending at this abutment, faded out there: this one's change in full)
        let a = pin(vec![[0.0, 0.0], [100.0, 0.0]], vec![1.0, 1.0], vec![0.0, 0.0], vec![1.0, 1.0], vec![1.0, 1.0], 5.0);
        let b = pin(vec![[-100.0, 0.0], [0.0, 0.0]], vec![0.4, 0.4], vec![0.0, 0.0], vec![1.0, 0.0], vec![1.0, 1.0], 5.0);
        let got = field_core(&[[0.0, 0.0]], &[0.0], &[a, b], &PRM);
        assert!((got[0] - 1.0).abs() < 1e-12);
    }
}
