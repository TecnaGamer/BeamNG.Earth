//! (644, M5 round 8, BNE_ROADSYS=surface 13; road shape first) A mapped road's embankment in the surface model's ground.
//!
//! Outside the national lidar the elevation is Copernicus GLO-30, a surface model, and dsmground's opening takes out what
//! is narrower than its window: buildings and tree stands, and also a road's own embankment, a ridge 30-80 m wide in the
//! 30 m model along the road (0bff: the trunk's embankment cut 7-9 m, the road graded into a trench and its 32 m bridge
//! pinned into it at 18-20 %, where the raw model and the street photos have the road high and gentle). embank_lift:
//! per road sample, the height the opening took from it that is the road's own: where the sample's ground is the
//! opening's (fine within match_m of the filtered z12: no lidar there), the raw model's cross-section bulges at the road
//! (the centre over the mean of its two sides by crest_min, in full from crest_full: an embankment on a ridge or on a
//! hillside alike; a street between blocks or trees, a trough, never), no building lies near (near), and the
//! cut (raw - filtered) lies between cut_min and max_lift: w x cut, w the crest's smoothstep. 0 elsewhere. The road's
//! plans read their ground plus it (grading's obs), the terrain follows the plans (an embankment); nothing away from the
//! roads changes. Pure: IEEE + - * / only, fixed order.
use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub struct Prm {
    pub match_m: f64,
    pub cut_min: f64,
    pub crest_min: f64,
    pub crest_full: f64,
    pub max_lift: f64,
}

fn smooth(f: f64) -> f64 {
    let f = if f < 0.0 { 0.0 } else if f > 1.0 { 1.0 } else { f };
    f * f * (3.0 - 2.0 * f)
}

/// The embankment lift per sample (see the module's notes).
pub fn lift(fine: &[f64], g12: &[f64], raw_c: &[f64], raw_l: &[f64], raw_r: &[f64], near: &[bool], p: &Prm) -> Vec<f64> {
    (0..fine.len()).map(|i| {
        let (f, g, c, l, r) = (fine[i], g12[i], raw_c[i], raw_l[i], raw_r[i]);
        if !(f.is_finite() && g.is_finite() && c.is_finite() && l.is_finite() && r.is_finite()) || near[i] {
            return 0.0;
        }
        let dm = f - g;
        if !(dm <= p.match_m && -dm <= p.match_m) {
            return 0.0;
        }
        let cut = c - g;
        // (the bulge over the sides' mean: 0bff's embankment runs along a hillside, its uphill side over it)
        let crest = c - (l + r) * 0.5;
        // (a cut deeper than max_lift is no embankment: a tree stand's or a hilltop town's 15-30 m, X8 at 0bff's hills)
        if !(cut > p.cut_min) || !(cut <= p.max_lift) || !(crest > p.crest_min) {
            return 0.0;
        }
        w_of(crest, p) * cut
    }).collect()
}

fn w_of(crest: f64, p: &Prm) -> f64 {
    smooth((crest - p.crest_min) / (p.crest_full - p.crest_min))
}

/// embank_lift(fine, filtered z12, raw z12 at the road, raw left, raw right, near a building, (match_m, cut_min,
/// crest_min, crest_full, max_lift)) -> the lift per sample (m).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn embank_lift<'py>(py: Python<'py>, fine: PyReadonlyArray1<'py, f64>, g12: PyReadonlyArray1<'py, f64>,
                        raw_c: PyReadonlyArray1<'py, f64>, raw_l: PyReadonlyArray1<'py, f64>,
                        raw_r: PyReadonlyArray1<'py, f64>, near: PyReadonlyArray1<'py, bool>,
                        prm: (f64, f64, f64, f64, f64)) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (f, g, c, l, r, n) = (fine.as_slice()?, g12.as_slice()?, raw_c.as_slice()?, raw_l.as_slice()?, raw_r.as_slice()?,
                              near.as_slice()?);
    let k = f.len();
    if g.len() != k || c.len() != k || l.len() != k || r.len() != k || n.len() != k {
        return Err(PyValueError::new_err("embank_lift: arrays of one length"));
    }
    let p = Prm { match_m: prm.0, cut_min: prm.1, crest_min: prm.2, crest_full: prm.3, max_lift: prm.4 };
    Ok(PyArray1::from_vec(py, lift(f, g, c, l, r, n, &p)))
}

/// The lifts along a road (arc m s, ascending) as one embankment: a gap of no lift up to gap_m long between two lifted
/// samples filled linearly between them (a building or a side road beside a few metres of it: the embankment goes on;
/// alone, its samples left at their ground made a 7 m notch, 0bff), then limited to rise or fall at most grade per m
/// along the road (the largest such profile under it: a run's ends taper, never a step). Pure.
pub fn along(s: &[f64], lift: &[f64], gap_m: f64, grade: f64) -> Vec<f64> {
    let n = lift.len().min(s.len());
    let mut out: Vec<f64> = lift[..n].to_vec();
    let on: Vec<usize> = (0..n).filter(|&i| out[i] > 0.0).collect();
    for w in on.windows(2) {
        let (a, b) = (w[0], w[1]);
        if b > a + 1 && s[b] - s[a] <= gap_m {
            for i in a + 1..b {
                let u = (s[i] - s[a]) / (s[b] - s[a]);
                out[i] = out[a] + (out[b] - out[a]) * u;
            }
        }
    }
    // (the largest profile under it with |slope| <= grade: one pass each way)
    for i in 1..n {
        let c = out[i - 1] + grade * (s[i] - s[i - 1]);
        if out[i] > c { out[i] = c; }
    }
    for i in (0..n.saturating_sub(1)).rev() {
        let c = out[i + 1] + grade * (s[i + 1] - s[i]);
        if out[i] > c { out[i] = c; }
    }
    out
}

/// embank_along(arc m (n,), lifts (n,), gap_m, grade) -> the lifts as one embankment (see along).
#[pyfunction]
pub fn embank_along<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, lift: PyReadonlyArray1<'py, f64>, gap_m: f64,
                         grade: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (s, l) = (s.as_slice()?, lift.as_slice()?);
    if s.len() != l.len() {
        return Err(PyValueError::new_err("embank_along: arrays of one length"));
    }
    Ok(PyArray1::from_vec(py, along(s, l, gap_m, grade)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_embankment_goes_on_past_a_short_gap_and_tapers_at_its_ends() {
        let s: Vec<f64> = (0..11).map(|i| i as f64 * 10.0).collect();
        let l = vec![0.0, 0.0, 6.0, 6.0, 0.0, 0.0, 6.0, 6.0, 0.0, 0.0, 0.0];
        // the 30 m gap filled; the ends at most 0.6 per m: kept
        assert_eq!(along(&s, &l, 30.0, 1.0), vec![0.0, 0.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 0.0, 0.0, 0.0]);
        // at most 0.2 per m: the ends taper
        let got = along(&s, &l, 30.0, 0.2);
        let want = [0.0, 0.0, 2.0, 4.0, 6.0, 6.0, 4.0, 2.0, 0.0, 0.0, 0.0];
        assert!(got.iter().zip(want.iter()).all(|(a, b)| (a - b).abs() < 1e-9), "{:?}", got);
        // a longer gap stays
        assert_eq!(along(&s, &l, 15.0, 1.0)[4], 0.0);
    }

    #[test]
    fn an_embankment_on_the_crest_is_given_back() {
        let p = Prm { match_m: 0.5, cut_min: 0.5, crest_min: 1.0, crest_full: 2.0, max_lift: 10.0 };
        // 0bff: filtered 68.4, raw 76.6 at the road, 75.0 / 73.2 beside: cut 8.2, bulge 2.5 (full)
        let got = lift(&[68.4], &[68.4], &[76.6], &[75.0], &[73.2], &[false], &p);
        assert!((got[0] - 8.2).abs() < 1e-9, "{:?}", got);
        // ... and on its hillside: 75.5 at the road, 75.3 uphill, 71.5 downhill (bulge 2.1)
        assert!((lift(&[68.0], &[68.0], &[75.5], &[75.3], &[71.5], &[false], &p)[0] - 7.5).abs() < 1e-9);
        // a trough (a street between blocks, a road through a tree stand): nothing
        assert_eq!(lift(&[68.4], &[68.4], &[70.0], &[75.0], &[73.2], &[false], &p)[0], 0.0);
        // a building near, lidar under it (fine off the filtered z12), a small cut: nothing
        assert_eq!(lift(&[68.4], &[68.4], &[76.6], &[75.0], &[73.2], &[true], &p)[0], 0.0);
        assert_eq!(lift(&[71.0], &[68.4], &[76.6], &[75.0], &[73.2], &[false], &p)[0], 0.0);
        assert_eq!(lift(&[68.4], &[68.4], &[68.8], &[67.0], &[67.0], &[false], &p)[0], 0.0);
        // a hilltop's 26 m: no embankment; a weak crest: part of it
        assert_eq!(lift(&[99.0], &[99.0], &[125.0], &[123.0], &[123.5], &[false], &p)[0], 0.0);
        let w = lift(&[68.4], &[68.4], &[76.6], &[76.0], &[74.6], &[false], &p)[0];
        assert!(w > 0.0 && w < 8.2, "{}", w);
    }
}
