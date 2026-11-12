//! Approach ramps and the raise round a raised junction (grading.approach_add, _Planner._junction_lift /
//! _raise_junctions / _end_raised, and the deck estimate of _lower_underpasses).
//!
//! approach_add: m to add to a road plan rising to `lift` at one end (a bridge's abutment, a raised junction), a
//! cosine ramp along the road whose steepest grade is `grade`, at most what keeps the fill slope (side_run x the height)
//! clear of the other roads beside it (gap to spare), a cap falling off at cap_grade along the road. fill_z (the plan's
//! own heights, optional): the ramp is a surface, not an offset: where the road climbs above its own height at the
//! raised end, the add is less by that climb (a junction raised 9.9 m over a pond stood its arm, which climbs 8.6 m to
//! the next junction of a diverging diamond, 18 m over the ground there: Wesley Chapel's SR 56 over I-75, GitHub #1043).
//!
//! junction_arm_raise: what is left at a junction's arm end of an approach's cosine ramp (its abutment's lift, the
//! stub's length), less what the stub climbs from its abutment's ground to the junction's (a surface, as fill_z: an
//! abutment mapped down in a cut, a junction on the ground beside it, Vienna's S1 under Rautenweg, GitHub #1083), and
//! never more over the arm end's own ground than over the junction's: an arm end lower than the junction gets the
//! ramp's offset, one higher only what reaches the junction's height (the same rule).
//!
//! stub_carried_raise: how far an abutment whose short approach ends at a junction another deck raises is carried up
//! with it (the approach climbs at ramp_grade at most from the junction), up to its own lift.
//!
//! under_deck_reach: a deck's height over a crossing as the dips under it see it: the straight line between its
//! raised ends, or, higher, the most it reaches climbing `grade` from each end after `flat` m (the abutment lifts are
//! asked so: grading.bridge_lifts), the lowest over the crossing's corridor.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// m: the Gaussian along a road its fill_z climb is smoothed over (approach_add_core)
const FILL_SMOOTH_M: f64 = 10.0;

/// The approach ramp (module doc); None when nothing is raised.
#[allow(clippy::too_many_arguments)]
pub fn approach_add_core(xy: &[[f64; 2]], at_end: bool, lift: f64, half: f64, others: &[(&[[f64; 2]], f64)],
                         side_run: f64, grade: Option<f64>, min_lift: f64, base_grade: f64, min_run: f64,
                         max_run: f64, gap: f64, cap_grade: f64, fill_z: Option<&[f64]>) -> Option<Vec<f64>> {
    let n = xy.len();
    if n < 2 || lift < min_lift {
        return None;
    }
    let mut s = vec![0.0f64; n];
    for i in 1..n {
        s[i] = s[i - 1] + (xy[i][0] - xy[i - 1][0]).hypot(xy[i][1] - xy[i - 1][1]);
    }
    let total = s[n - 1];
    let g = grade.unwrap_or(base_grade);
    let hi = max_run * grade.map_or(1.0, |gr| base_grade / gr);
    let run = (lift * std::f64::consts::PI / (2.0 * g)).max(min_run).min(hi);
    let mut add: Vec<f64> = (0..n).map(|i| {
        let d = if at_end { total - s[i] } else { s[i] };
        let u = (d / run).clamp(0.0, 1.0);
        lift * 0.5 * (1.0 + (std::f64::consts::PI * u).cos())
    }).collect();
    let on: Vec<usize> = (0..n).filter(|&i| add[i] > 1e-3).collect();
    if on.is_empty() {
        return None;
    }
    let (mut lox, mut loy, mut hix, mut hiy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for &i in &on {
        lox = lox.min(xy[i][0]);
        loy = loy.min(xy[i][1]);
        hix = hix.max(xy[i][0]);
        hiy = hiy.max(xy[i][1]);
    }
    let sr = side_run.max(1e-6);
    for &(line, oh) in others {
        if line.len() < 2 {
            continue;
        }
        let reach = half + oh + gap + side_run * lift;
        let (mut mx, mut my, mut nx, mut ny) = (f64::NEG_INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::INFINITY);
        for p in line {
            mx = mx.max(p[0]);
            my = my.max(p[1]);
            nx = nx.min(p[0]);
            ny = ny.min(p[1]);
        }
        if mx < lox - reach || my < loy - reach || nx > hix + reach || ny > hiy + reach {
            continue;                   // too far to matter
        }
        for &i in &on {
            let q = xy[i];
            let mut dist = f64::INFINITY;
            for w in line.windows(2) {
                let (a, b) = (w[0], w[1]);
                let ab = [b[0] - a[0], b[1] - a[1]];
                let l2 = (ab[0] * ab[0] + ab[1] * ab[1]).max(1e-12);
                let t = (((q[0] - a[0]) * ab[0] + (q[1] - a[1]) * ab[1]) / l2).clamp(0.0, 1.0);
                dist = dist.min((q[0] - (a[0] + t * ab[0])).hypot(q[1] - (a[1] + t * ab[1])));
            }
            let cap = ((dist - half - oh - gap) / sr).max(0.0);
            add[i] = add[i].min(cap);
        }
    }
    // a cap lowers the ramp before it at cap_grade at most, instead of a step at the cap
    for i in 1..n {
        add[i] = add[i].min(add[i - 1] + cap_grade * (s[i] - s[i - 1]));
    }
    for i in (0..n - 1).rev() {
        add[i] = add[i].min(add[i + 1] + cap_grade * (s[i + 1] - s[i]));
    }
    if let Some(z) = fill_z {
        let z0 = if at_end { z[n - 1] } else { z[0] };
        // what the road's own climb takes off the ramp; smoothed along it (FILL_SMOOTH_M, 238): where the ramp met a road
        // climbing away from the bridge it left a sag, the grade turning 8 % in a metre (the A6 at Heilbronn, GitHub
        // #1176), now a rounded one; held exact at the raised end
        let r: Vec<f64> = (0..n).map(|i| add[i].min((z[i] - z0).max(0.0))).collect();
        let sig = FILL_SMOOTH_M;
        for i in 0..n {
            let d_end = if at_end { total - s[i] } else { s[i] };
            // (a Gaussian-weighted straight line fitted round each vertex: a steady climb kept as it is, also where the
            // window runs off the line's end, a kink rounded)
            let (mut s0, mut s1, mut s2, mut r0, mut r1) = (0.0f64, 0.0f64, 0.0f64, 0.0f64, 0.0f64);
            for j in 0..n {
                let ds = s[j] - s[i];
                if ds.abs() > 3.0 * sig {
                    continue;
                }
                // (the vertices' own spacing as their weight: an uneven plan line is no lopsided average)
                let span = 0.5 * (s[(j + 1).min(n - 1)] - s[j.saturating_sub(1)]).max(1e-3);
                let w = (-0.5 * (ds / sig) * (ds / sig)).exp() * span;
                s0 += w;
                s1 += w * ds;
                s2 += w * ds * ds;
                r0 += w * r[j];
                r1 += w * r[j] * ds;
            }
            let den = s0 * s2 - s1 * s1;
            let rs = if den > 1e-9 * s0 * s0.max(1.0) { (s2 * r0 - s1 * r1) / den } else if s0 > 0.0 { r0 / s0 } else { r[i] };
            let hold = (d_end / (2.0 * sig)).min(1.0);       // (none at the raised end itself)
            let rr = (r[i] + (rs - r[i]) * hold).max(0.0);
            add[i] = (add[i] - rr).max(0.0);
        }
    }
    if add.iter().cloned().fold(f64::NEG_INFINITY, f64::max) < 1e-3 {
        return None;
    }
    Some(add)
}

/// grading.approach_add (module doc). xy (n, 2); others: their points (m, 2), each line's rows off[k]..off[k + 1],
/// half widths (k,); grade <= 0: base_grade; fill_z: (n,) or empty (none). None when nothing is raised.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn approach_add<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, at_end: bool, lift: f64, half: f64,
                         oxy: PyReadonlyArray2<'py, f64>, ooff: PyReadonlyArray1<'py, i64>,
                         ohalf: PyReadonlyArray1<'py, f64>, side_run: f64, grade: f64, min_lift: f64,
                         base_grade: f64, min_run: f64, max_run: f64, gap: f64, cap_grade: f64,
                         fill_z: PyReadonlyArray1<'py, f64>) -> PyResult<Option<Bound<'py, PyArray1<f64>>>> {
    let (a, oa, oo, oh, fz) = (xy.as_array(), oxy.as_array(), ooff.as_array(), ohalf.as_array(), fill_z.as_array());
    let k = if oo.is_empty() { 0 } else { oo.len() - 1 };
    if a.ncols() < 2 || (oa.nrows() > 0 && oa.ncols() < 2) || oh.len() != k
        || (k > 0 && (oo[0] != 0 || oo[k] as usize != oa.nrows() || (1..oo.len()).any(|i| oo[i] < oo[i - 1])))
        || (!fz.is_empty() && fz.len() != a.nrows()) {
        return Err(PyValueError::new_err("approach_add: mismatched arrays"));
    }
    let pts: Vec<[f64; 2]> = (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect();
    let opts: Vec<[f64; 2]> = (0..oa.nrows()).map(|i| [oa[[i, 0]], oa[[i, 1]]]).collect();
    let fzv: Vec<f64> = fz.iter().copied().collect();
    let others: Vec<(&[[f64; 2]], f64)> = (0..k).map(|j| (&opts[oo[j] as usize..oo[j + 1] as usize], oh[j])).collect();
    let got = approach_add_core(&pts, at_end, lift, half, &others, side_run, if grade > 0.0 { Some(grade) } else { None },
                                min_lift, base_grade, min_run, max_run, gap, cap_grade,
                                if fzv.is_empty() { None } else { Some(&fzv) });
    Ok(got.map(|v| v.into_pyarray(py)))
}

/// The raise at a junction's arm end (module doc): per candidate approach its abutment's lift and stub length, the
/// ground at the abutment (z_b), at the stub's junction end (z_far) and at the arm end (z_e). (best m, its candidate)
/// or None under min_lift.
#[allow(clippy::too_many_arguments)]
pub fn junction_arm_raise_core(lift: &[f64], stub: &[f64], z_b: &[f64], z_far: &[f64], z_e: f64, grade: f64,
                               min_run: f64, max_run: f64, min_lift: f64) -> Option<(f64, usize)> {
    let mut best: Option<(f64, usize)> = None;
    for i in 0..lift.len() {
        let run = (lift[i] * std::f64::consts::PI / (2.0 * grade)).clamp(min_run, max_run);
        let v = lift[i] * 0.5 * (1.0 + (std::f64::consts::PI * (stub[i] / run).min(1.0)).cos())
            - (z_far[i] - z_b[i]).max(0.0) + (z_far[i] - z_e).min(0.0);
        if v >= min_lift && best.map_or(true, |(b, _)| v > b) {
            best = Some((v, i));
        }
    }
    best
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn junction_arm_raise(lift: PyReadonlyArray1<'_, f64>, stub: PyReadonlyArray1<'_, f64>,
                          z_b: PyReadonlyArray1<'_, f64>, z_far: PyReadonlyArray1<'_, f64>, z_e: f64, grade: f64,
                          min_run: f64, max_run: f64, min_lift: f64) -> PyResult<Option<(f64, usize)>> {
    let (l, s, b, z) = (lift.as_array(), stub.as_array(), z_b.as_array(), z_far.as_array());
    if l.len() != s.len() || l.len() != z.len() || l.len() != b.len() {
        return Err(PyValueError::new_err("junction_arm_raise: mismatched arrays"));
    }
    let v = |a: numpy::ndarray::ArrayView1<f64>| a.iter().copied().collect::<Vec<f64>>();
    Ok(junction_arm_raise_core(&v(l), &v(s), &v(b), &v(z), z_e, grade, min_run, max_run, min_lift))
}

/// m an abutment (ground z_b, asked lift) is raised with the junction at its short approach's other end (ground z_far,
/// raised v there), the approach stub m long climbing ramp_grade at most: never more than its lift, never less than own.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn stub_carried_raise(own: f64, lift: f64, z_b: f64, z_far: f64, v: f64, stub: f64, ramp_grade: f64) -> f64 {
    own.max(lift.min(z_far + v + ramp_grade * stub - z_b))
}

/// The deck over a crossing as the dips see it (module doc): ends' heights ze0 / ze1 of a deck length m long, the
/// crossing's corridor arcs lo..hi (m from ze0's end); climb off: the straight line alone.
pub fn under_deck_reach_core(ze0: f64, ze1: f64, length: f64, lo: f64, hi: f64, grade: f64, flat: f64, climb: bool)
                             -> f64 {
    let at = |c: f64| {
        let lin = ze0 + (ze1 - ze0) * c / length.max(1e-9);
        if !climb {
            return lin;
        }
        let up = (ze0 + grade * (c - flat).max(0.0)).min(ze1 + grade * (length - c - flat).max(0.0));
        lin.max(up)
    };
    let steps = ((hi - lo) / 1.0).ceil().max(1.0) as usize;
    (0..=steps).map(|k| at(lo + (hi - lo) * k as f64 / steps as f64)).fold(f64::INFINITY, f64::min)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn under_deck_reach(ze0: f64, ze1: f64, length: f64, lo: f64, hi: f64, grade: f64, flat: f64, climb: bool) -> f64 {
    under_deck_reach_core(ze0, ze1, length, lo, hi, grade, flat, climb)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(n: usize, step: f64) -> Vec<[f64; 2]> {
        (0..n).map(|i| [i as f64 * step, 0.0]).collect()
    }

    #[test]
    fn ramp_falls_from_the_lift() {
        let xy = line(41, 5.0);
        let a = approach_add_core(&xy, false, 6.0, 4.0, &[], 2.0, None, 0.5, 0.05, 30.0, 320.0, 1.0, 0.1, None).unwrap();
        assert!((a[0] - 6.0).abs() < 1e-9);
        assert!(a.windows(2).all(|w| w[1] <= w[0] + 1e-12));
        let r = approach_add_core(&xy.iter().rev().copied().collect::<Vec<_>>(), true, 6.0, 4.0, &[], 2.0, None, 0.5,
                                  0.05, 30.0, 320.0, 1.0, 0.1, None).unwrap();
        assert!((r[40] - 6.0).abs() < 1e-9);
    }

    #[test]
    fn a_road_beside_caps_it() {
        let xy = line(41, 5.0);
        let beside: Vec<[f64; 2]> = vec![[0.0, 8.0], [200.0, 8.0]];
        let a = approach_add_core(&xy, false, 6.0, 4.0, &[(&beside, 3.0)], 2.0, None, 0.5, 0.05, 30.0, 320.0, 1.0, 0.1,
                                  None).unwrap();
        assert!(a[0] <= 0.0 + 1e-9, "8 m off, 4 + 3 + 1 m of it taken: nothing left for the slope");
    }

    #[test]
    fn a_fill_meets_a_climbing_road_in_a_rounded_sag() {
        // the A6 at Heilbronn (GitHub #1176): a 3.1 m abutment lift, the road climbing 3.5 % away from the bridge; the
        // ramp's surface met it 48 m on with the grade turning 8 % in a metre
        let xy = line(101, 1.0);
        let z: Vec<f64> = (0..101).map(|i| 204.8 + 0.035 * i as f64).collect();
        let a = approach_add_core(&xy, false, 3.1, 4.0, &[], 2.0, None, 0.5, 0.05, 30.0, 320.0, 1.0, 0.1, Some(&z))
            .unwrap();
        assert!((a[0] - 3.1).abs() < 1e-9);
        let top: Vec<f64> = a.iter().zip(&z).map(|(a, z)| a + z).collect();
        let worst = (1..100).map(|i| (top[i + 1] - 2.0 * top[i] + top[i - 1]).abs()).fold(0.0f64, f64::max);
        assert!(worst < 0.01, "grade change per metre {worst}");
    }

    #[test]
    fn fill_stops_where_the_road_climbs_above_its_end() {
        let xy = line(11, 2.5);                     // 25 m: a junction's arm climbing 8.6 m to the next junction
        let z: Vec<f64> = (0..11).map(|i| 26.0 + 0.86 * i as f64).collect();
        let a = approach_add_core(&xy, false, 9.9, 4.0, &[], 2.0, None, 0.5, 0.05, 30.0, 320.0, 1.0, 0.1, Some(&z))
            .unwrap();
        assert!((a[0] - 9.9).abs() < 1e-9);
        let top: Vec<f64> = a.iter().zip(&z).map(|(a, z)| a + z).collect();
        assert!(top.iter().all(|&t| t <= 26.0 + 9.9 + 1e-9), "never over the raised end's height");
        assert!(a[10] < 9.9 - 8.0, "the arm's far end, 8.6 m up, gets little: {}", a[10]);
    }

    #[test]
    fn arm_end_higher_than_the_junction_gets_only_the_junction_height() {
        let got = junction_arm_raise_core(&[9.9], &[8.0], &[26.3], &[26.0], 34.6, 0.05, 30.0, 320.0, 0.5).unwrap();
        assert!(got.0 < 1.4 && got.0 > 1.0, "{}", got.0);
        let low = junction_arm_raise_core(&[5.3], &[8.0], &[30.0], &[30.0], 24.0, 0.05, 30.0, 320.0, 0.5).unwrap();
        assert!((low.0 - junction_arm_raise_core(&[5.3], &[8.0], &[30.0], &[30.0], 30.0, 0.05, 30.0, 320.0, 0.5)
            .unwrap().0).abs() < 1e-12, "a lower arm end: the ramp's offset, as before");
        let cut = junction_arm_raise_core(&[10.0], &[10.0], &[153.9], &[160.5], 160.5, 0.05, 30.0, 320.0, 0.5).unwrap();
        assert!(cut.0 < 10.0 - 6.0, "an abutment down in a cut: the stub's climb out of it taken off");
    }

    #[test]
    fn deck_reach_climbs_from_its_ends() {
        let flat_line = under_deck_reach_core(35.0, 37.0, 96.0, 26.0, 42.0, 0.05, 20.0, false);
        let climbed = under_deck_reach_core(35.0, 37.0, 96.0, 26.0, 42.0, 0.05, 20.0, true);
        assert!((flat_line - (35.0 + 2.0 * 26.0 / 96.0)).abs() < 1e-9);
        assert!(climbed > flat_line);
        // a short deck: within the flat run of both ends, the straight line
        assert!((under_deck_reach_core(35.0, 35.0, 30.0, 10.0, 20.0, 0.05, 20.0, true) - 35.0).abs() < 1e-9);
    }
}
