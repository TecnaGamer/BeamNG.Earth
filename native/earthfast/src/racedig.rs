//! A tunnel part dug under a racetrack (server racedig.py, BNE_RACEWAY_DIG; road revamp R1k, from PR #992's "the track
//! surface wins"): the track keeps its surface and the tube goes under it. Its floor (s: arc lengths, floor: the
//! tunnel's solved floor, ends at its portals / joints) is lowered to `need` (the floor the tube needs under the track,
//! +inf where the track is not over it) and to the ramps rising from there at `grade` towards the ends; the ends stay
//! where they are and the floor never falls faster than `max_grade` from either (a tube too short for the dig keeps
//! what fits), its grade from an end growing over the first `ease` m (a parabola: no kink where the road from the
//! portal meets the ramp). Never raised. The samples flagged `cover` (the track's band and the portal clearance past it) become a
//! covered section (kind 1, a low arch, where they were open), and open gaps shorter than `min_gap` m between covered
//! samples are covered too. Sums, products, min and max only: the same bits everywhere.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

/// How far the floor may fall at distance d from a pinned end: max_grade * d^2 / (2 ease) over the first ease m, then
/// max_grade's straight line on from there.
fn end_drop(d: f64, max_grade: f64, ease: f64) -> f64 {
    if ease > 0.0 && d < ease {
        max_grade * d * d / (2.0 * ease)
    } else {
        max_grade * (d - 0.5 * ease.max(0.0))
    }
}

#[allow(clippy::too_many_arguments)]
pub fn dig(s: &[f64], floor: &[f64], need: &[f64], cover: &[bool], kinds: &[i64], grade: f64, max_grade: f64,
           min_gap: f64, ease: f64) -> (Vec<f64>, Vec<i64>) {
    let n = s.len().min(floor.len()).min(need.len()).min(cover.len()).min(kinds.len());
    let mut out: Vec<f64> = floor[..n].to_vec();
    let mut k: Vec<i64> = kinds[..n].to_vec();
    if n == 0 {
        return (out, k);
    }
    // the ramps from every need point: the lower envelope of need[j] + grade * |s - s[j]| (two sweeps)
    let mut e: Vec<f64> = need[..n].to_vec();
    for i in 1..n {
        let v = e[i - 1] + grade * (s[i] - s[i - 1]);
        if v < e[i] {
            e[i] = v;
        }
    }
    for i in (0..n - 1).rev() {
        let v = e[i + 1] + grade * (s[i + 1] - s[i]);
        if v < e[i] {
            e[i] = v;
        }
    }
    let (f0, f1, last) = (floor[0], floor[n - 1], s[n - 1]);
    for i in 0..n {
        let mut z = out[i].min(e[i]);
        // the ends pinned: no steeper than max_grade down from either, never over the floor as solved
        let lb = (f0 - end_drop(s[i] - s[0], max_grade, ease))
            .max(f1 - end_drop(last - s[i], max_grade, ease)).min(out[i]);
        if lb > z {
            z = lb;
        }
        out[i] = z;
    }
    out[0] = f0;
    out[n - 1] = f1;
    for i in 0..n {
        if cover[i] && k[i] == 0 {
            k[i] = 1;
        }
    }
    // short open gaps between covered samples closed (tunnel_sections' SECTION_MIN_M, as its own merge)
    let mut i = 0;
    while i < n {
        if k[i] != 0 {
            i += 1;
            continue;
        }
        let a = i;
        while i < n && k[i] == 0 {
            i += 1;
        }
        // open run a..i (exclusive)
        if a > 0 && i < n {
            let len = s[i] - s[a - 1];
            if len < min_gap {
                let fill = k[a - 1].min(k[i]).max(1);
                for v in k.iter_mut().take(i).skip(a) {
                    *v = fill;
                }
            }
        }
    }
    (out, k)
}

/// racedig.dig_floor: (floor, kinds) of a tunnel part dug under a racetrack (above).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn race_dig_floor<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, floor: PyReadonlyArray1<'py, f64>,
                           need: PyReadonlyArray1<'py, f64>, cover: PyReadonlyArray1<'py, bool>,
                           kinds: PyReadonlyArray1<'py, i64>, grade: f64, max_grade: f64, min_gap: f64, ease: f64)
                           -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>) {
    let (s, f, nd, c, k) = (s.as_array().to_vec(), floor.as_array().to_vec(), need.as_array().to_vec(),
                            cover.as_array().to_vec(), kinds.as_array().to_vec());
    let (out, kd) = dig(&s, &f, &nd, &c, &k, grade, max_grade, min_gap, ease);
    (out.into_pyarray(py), kd.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dug_under_the_track_ramps_and_ends_kept() {
        // a 100 m part, 2 m steps, flat floor at 0, the track over 40-60 m wants the floor at -5
        let s: Vec<f64> = (0..51).map(|i| i as f64 * 2.0).collect();
        let f = vec![0.0; 51];
        let need: Vec<f64> = s.iter().map(|&x| if (40.0..=60.0).contains(&x) { -5.0 } else { f64::INFINITY }).collect();
        let cover: Vec<bool> = s.iter().map(|&x| (34.0..=66.0).contains(&x)).collect();
        let kinds = vec![0i64; 51];
        let (z, k) = dig(&s, &f, &need, &cover, &kinds, 0.12, 0.25, 8.0, 0.0);
        assert_eq!(z[0], 0.0);
        assert_eq!(z[50], 0.0);
        assert_eq!(z[25], -5.0);
        assert_eq!(z[20], -5.0);
        // 10 m before the band: 1.2 m up the ramp
        assert!((z[15] - (-3.8)).abs() < 1e-12);
        for w in z.windows(2) {
            assert!((w[1] - w[0]).abs() <= 0.25 * 2.0 + 1e-12);
        }
        assert!(k[17..=33].iter().all(|&v| v == 1));
        assert_eq!(k[0], 0);
    }

    #[test]
    fn short_tube_keeps_what_fits() {
        let s: Vec<f64> = (0..11).map(|i| i as f64 * 2.0).collect();       // 20 m
        let f = vec![0.0; 11];
        let need: Vec<f64> = (0..11).map(|i| if (4..=6).contains(&i) { -6.0 } else { f64::INFINITY }).collect();
        let (z, _k) = dig(&s, &f, &need, &[false; 11], &[1; 11], 0.12, 0.25, 8.0, 0.0);
        // 8 m from each end at 25 %: 2 m down at most
        assert!((z[5] - (-2.5)).abs() < 1e-12);
        assert!(z.iter().zip(f.iter()).all(|(a, b)| a <= b));
    }

    #[test]
    fn never_raised_and_gaps_closed() {
        let s: Vec<f64> = (0..11).map(|i| i as f64).collect();
        let f: Vec<f64> = (0..11).map(|i| -(i as f64) * 0.1).collect();
        let need = vec![f64::INFINITY; 11];
        let kinds = vec![1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0];
        let (z, k) = dig(&s, &f, &need, &[false; 11], &kinds, 0.12, 0.25, 8.0, 0.0);
        assert_eq!(z, f);
        assert_eq!(k, vec![1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0]);
    }
}
