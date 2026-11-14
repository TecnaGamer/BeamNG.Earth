//! A sidewalk run's furniture stations (server furniture._stations): the road's stations phase + k spacing (arc
//! length along the road line, so every tile and anchor gets the same ones) found on the run, END_CLEAR_M from its
//! ends, each with its away-from-the-road unit and its arc length back on the road line (the station's k). The
//! numbers are furniture._project / _at / _arclen's numpy steps to the bit, in their order (np.hypot is libm's
//! hypot, cumsum sequential, argmin the first minimum or the first NaN); one call per run instead of a numpy
//! round per station (2026-11-10: _stations and the k projections were most of furniture.place's own Python).
use crate::dm::Dm;
use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A polyline's rows (k, c), c >= 2.
struct Line<'a> {
    v: &'a [f64],
    c: usize,
}

impl Line<'_> {
    fn n(&self) -> usize {
        self.v.len() / self.c
    }
    fn at(&self, i: usize, j: usize) -> f64 {
        self.v[i * self.c + j]
    }
    /// _arclen: [0, cumsum(hypot(diff))].
    fn arclen(&self) -> Vec<f64> {
        let n = self.n();
        let mut s = Vec::with_capacity(n);
        s.push(0.0);
        let mut acc = 0.0;
        for i in 1..n {
            let d = (self.at(i, 0) - self.at(i - 1, 0)).dhypot(self.at(i, 1) - self.at(i - 1, 1));
            acc = if i == 1 { d } else { acc + d };
            s.push(acc);
        }
        s
    }
}

fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    // np.clip: NaN through
    if x.is_nan() {
        x
    } else if x < lo {
        lo
    } else if x > hi {
        hi
    } else {
        x
    }
}

/// _project(line, q) for one point: the arc length along line of the nearest point to q.
fn project(line: &Line, ss: &[f64], qx: f64, qy: f64) -> f64 {
    let n = line.n();
    let mut best = f64::INFINITY;
    let mut bk = 0usize;
    let mut bt = 0.0;
    let mut bl2 = 0.0;
    let mut have = false;
    for k in 0..n.saturating_sub(1) {
        let (ax, ay) = (line.at(k, 0), line.at(k, 1));
        let (dx, dy) = (line.at(k + 1, 0) - ax, line.at(k + 1, 1) - ay);
        let s2 = dx * dx + dy * dy;
        let l2 = if s2.is_nan() || s2 > 1e-12 { s2 } else { 1e-12 };    // (np.maximum)
        let t = clip(((qx - ax) * dx + (qy - ay) * dy) / l2, 0., 1.);
        let (px, py) = (ax + t * dx, ay + t * dy);
        let dist = (qx - px).dhypot(qy - py);
        if !have || dist.is_nan() || dist < best {
            best = dist;
            bk = k;
            bt = t;
            bl2 = l2;
            have = true;
            if dist.is_nan() {
                break;          // (argmin: the first NaN)
            }
        }
    }
    if !have {
        return f64::NAN;
    }
    ss[bk] + bt * bl2.sqrt()
}

/// _at(line, s) for one arc length: (the point (c,), the unit direction (2,)).
fn at(line: &Line, ss: &[f64], s: f64, p: &mut Vec<f64>) -> (f64, f64) {
    let n = line.n();
    let s = clip(s, 0., ss[n - 1]);
    // searchsorted(ss, s, "right") - 1, clipped to [0, n - 2]
    let right = if s.is_nan() { n } else { ss.partition_point(|&v| v <= s) };
    let k = (right as i64 - 1).clamp(0, n as i64 - 2) as usize;
    let d = ss[k + 1] - ss[k];
    let l = if d.is_nan() || d > 1e-9 { d } else { 1e-9 };
    let t = (s - ss[k]) / l;
    p.clear();
    for j in 0..line.c {
        let seg = line.at(k + 1, j) - line.at(k, j);
        p.push(line.at(k, j) + seg * t);
    }
    ((line.at(k + 1, 0) - line.at(k, 0)) / l, (line.at(k + 1, 1) - line.at(k, 1)) / l)
}

/// (points (n, c) on the run, away-from-the-road units (n, 2), their arc lengths on the road line (n,)): run (k, c)
/// the sidewalk run, line (m, 2+) the road's centreline, side +1 / -1.
#[pyfunction]
pub fn furniture_stations<'py>(py: Python<'py>, run: PyReadonlyArray2<'py, f64>, line: PyReadonlyArray2<'py, f64>,
                               spacing: f64, phase: f64, clear: f64, side: f64)
                               -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                                            Bound<'py, PyArray1<f64>>)> {
    let (rc, lc) = (run.shape()[1], line.shape()[1]);
    if rc < 2 || lc < 2 || run.shape()[0] < 2 || line.shape()[0] < 2 {
        return Err(PyValueError::new_err("run and line must be (k >= 2, >= 2)"));
    }
    let run = Line { v: run.as_slice()?, c: rc };
    let line = Line { v: line.as_slice()?, c: lc };
    let got = py.detach(|| -> Result<(Vec<f64>, Vec<f64>, Vec<f64>), String> {
        let (ss_line, ss_run) = (line.arclen(), run.arclen());
        let mut pts: Vec<f64> = Vec::new();
        let mut outs: Vec<f64> = Vec::new();
        let mut sl: Vec<f64> = Vec::new();
        let nr = run.n();
        let a = project(&line, &ss_line, run.at(0, 0), run.at(0, 1));
        let b = project(&line, &ss_line, run.at(nr - 1, 0), run.at(nr - 1, 1));
        let (lo, hi) = (if b < a { b } else { a }, if b > a { b } else { a });     // (Python's min / max)
        let (k0, k1) = (((lo - phase) / spacing).ceil(), ((hi - phase) / spacing).floor());
        if !k0.is_finite() || !k1.is_finite() {         // (math.ceil / floor raise)
            return Err(format!("no stations on a run at arc lengths {lo} .. {hi}"));
        }
        if k1 < k0 {
            return Ok((pts, outs, sl));
        }
        let len = ss_run[nr - 1];
        let mut p = Vec::with_capacity(lc.max(rc));
        let mut k = k0 as i64;
        while k <= k1 as i64 {
            let s_road = phase + spacing * k as f64;
            k += 1;
            at(&line, &ss_line, s_road, &mut p);
            let s = project(&run, &ss_run, p[0], p[1]);
            if s < clear || s > len - clear {
                continue;
            }
            let (dx, dy) = at(&run, &ss_run, s, &mut p);
            sl.push(project(&line, &ss_line, p[0], p[1]));
            pts.extend_from_slice(&p);
            outs.push(-dy * side);
            outs.push(dx * side);
        }
        Ok((pts, outs, sl))
    });
    let (pts, outs, sl) = got.map_err(PyValueError::new_err)?;
    let n = sl.len();
    let pts = Array2::from_shape_vec((n, rc), pts).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let outs = Array2::from_shape_vec((n, 2), outs).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok((pts.into_pyarray(py), outs.into_pyarray(py), sl.into_pyarray(py)))
}
