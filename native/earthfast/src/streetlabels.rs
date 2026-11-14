//! Points to write a road's name at (the 3D map's street labels, server labels.street_labels): along each polyline in
//! lon / lat, one point every `spacing` metres (the first half a spacing in, a polyline shorter than a spacing gets
//! one at its middle), with the road's bearing there (degrees clockwise from north) for the text's direction.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const M_PER_DEG: f64 = 111_320.0;

/// lonlat: (n, 2); starts / ends: each polyline's [start, end) rows. Returns (feature, lon, lat, bearing) per point.
pub fn label_points(lonlat: &[[f64; 2]], starts: &[i64], ends: &[i64], spacing: f64)
                    -> (Vec<i64>, Vec<f64>, Vec<f64>, Vec<f64>) {
    let (mut fi, mut lo, mut la, mut br) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let spacing = if spacing > 1.0 { spacing } else { 1.0 };
    for (k, (&s, &e)) in starts.iter().zip(ends.iter()).enumerate() {
        let (s, e) = (s.max(0) as usize, (e.max(0) as usize).min(lonlat.len()));
        if e < s + 2 {
            continue;
        }
        let pts = &lonlat[s..e];
        let kx = M_PER_DEG * pts[0][1].to_radians().cos();
        // segment lengths in metres
        let seg: Vec<f64> = pts.windows(2)
            .map(|w| (((w[1][0] - w[0][0]) * kx).powi(2) + ((w[1][1] - w[0][1]) * M_PER_DEG).powi(2)).sqrt())
            .collect();
        let total: f64 = seg.iter().sum();
        if !(total > 0.0) {
            continue;
        }
        let mut targets = Vec::new();
        if total < spacing {
            targets.push(total / 2.0);
        } else {
            let mut d = spacing / 2.0;
            while d < total {
                targets.push(d);
                d += spacing;
            }
        }
        let (mut acc, mut j) = (0.0, 0usize);
        for t in targets {
            while j + 1 < seg.len() && acc + seg[j] < t {
                acc += seg[j];
                j += 1;
            }
            let f = if seg[j] > 0.0 { ((t - acc) / seg[j]).clamp(0.0, 1.0) } else { 0.0 };
            let (a, b) = (pts[j], pts[j + 1]);
            fi.push(k as i64);
            lo.push(a[0] + (b[0] - a[0]) * f);
            la.push(a[1] + (b[1] - a[1]) * f);
            let (dx, dy) = ((b[0] - a[0]) * kx, (b[1] - a[1]) * M_PER_DEG);
            br.push(dx.atan2(dy).to_degrees().rem_euclid(360.0));
        }
    }
    (fi, lo, la, br)
}

#[pyfunction]
pub fn polyline_label_points<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, starts: PyReadonlyArray1<'py, i64>,
                                  ends: PyReadonlyArray1<'py, i64>, spacing: f64)
                                  -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>,
                                      Bound<'py, PyArray1<f64>>) {
    let a = lonlat.as_array();
    let pts: Vec<[f64; 2]> = a.outer_iter().map(|r| [r[0], r[1]]).collect();
    let (s, e) = (starts.as_array().to_vec(), ends.as_array().to_vec());
    let (fi, lo, la, br) = py.detach(|| label_points(&pts, &s, &e, spacing));
    (fi.into_pyarray(py), lo.into_pyarray(py), la.into_pyarray(py), br.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spaced_points_and_bearing() {
        // a 1 km road due east at the equator (0.009 deg ~ 1002 m), and a 100 m one due north
        let pts = [[0.0, 0.0], [0.009, 0.0], [1.0, 0.0], [1.0, 0.0009]];
        let (fi, lo, _la, br) = label_points(&pts, &[0, 2], &[2, 4], 400.0);
        assert_eq!(fi, vec![0, 0, 0, 1]);               // 200, 600, 1000 m along the first; the middle of the second
        assert!((lo[0] * 111_320.0 - 200.0).abs() < 1.0);
        assert!((br[0] - 90.0).abs() < 1e-6 && br[3].abs() < 1e-6);
    }
}
