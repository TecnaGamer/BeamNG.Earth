//! climate.au_state's state lookup: the Australian states' outlines (even-odd over every ring) and, off their
//! coasts, the nearest state within a few kilometres, on points snapped to a 1/grid degree cell; climate._au_index's
//! arithmetic per segment. Exact against it: the crossing count only skips segments that cannot straddle the point's
//! latitude, and the coast search only those farther in latitude alone than the distance limit.
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::dm::Dm;
use std::collections::HashMap;

const BUCKET: f64 = 0.05; // degrees of latitude per bucket of segments

struct State {
    seg: Vec<[f64; 4]>, // x1, y1, x2, y2 (lon, lat)
    bb: [f64; 4],       // lon0, lat0, lon1, lat1
    y0: f64,
    buckets: Vec<Vec<u32>>, // the segments whose latitude range meets each bucket
}

impl State {
    fn bucket(&self, y: f64) -> isize {
        ((y - self.y0) / BUCKET).floor() as isize
    }
    /// The segments that may lie within latitudes [lo, hi], each once.
    fn near(&self, lo: f64, hi: f64, out: &mut Vec<u32>) {
        out.clear();
        let n = self.buckets.len() as isize;
        let (b0, b1) = (self.bucket(lo).max(0), self.bucket(hi).min(n - 1));
        if b0 > b1 {
            return;
        }
        for b in b0..=b1 {
            out.extend_from_slice(&self.buckets[b as usize]);
        }
        if b1 > b0 {
            out.sort_unstable();
            out.dedup();
        }
    }
}

#[pyclass(module = "earthfast")]
pub struct AuStates {
    states: Vec<State>,
}

#[pymethods]
impl AuStates {
    /// segs: per state its outline segments (4, n) (x1, y1, x2, y2 rows); bbs (states, 4) their boxes.
    #[new]
    fn new(segs: Vec<PyReadonlyArray2<f64>>, bbs: PyReadonlyArray2<f64>) -> PyResult<Self> {
        let bb = bbs.as_array();
        if bb.nrows() != segs.len() || bb.ncols() != 4 {
            return Err(PyValueError::new_err("AuStates: a (4,) box per state"));
        }
        let mut states = Vec::with_capacity(segs.len());
        for (si, e) in segs.iter().enumerate() {
            let e = e.as_array();
            if e.nrows() != 4 {
                return Err(PyValueError::new_err("AuStates: segments as (4, n)"));
            }
            let seg: Vec<[f64; 4]> = (0..e.ncols()).map(|k| [e[[0, k]], e[[1, k]], e[[2, k]], e[[3, k]]]).collect();
            let y0 = seg.iter().fold(f64::INFINITY, |m, s| m.min(s[1]).min(s[3]));
            let y1 = seg.iter().fold(f64::NEG_INFINITY, |m, s| m.max(s[1]).max(s[3]));
            let mut st = State { seg, bb: [bb[[si, 0]], bb[[si, 1]], bb[[si, 2]], bb[[si, 3]]], y0, buckets: vec![] };
            if st.seg.is_empty() {
                states.push(st);
                continue;
            }
            st.buckets = vec![vec![]; (st.bucket(y1) + 1) as usize];
            for (k, s) in st.seg.iter().enumerate() {
                let (b0, b1) = (st.bucket(s[1].min(s[3])), st.bucket(s[1].max(s[3])));
                for b in b0..=b1 {
                    st.buckets[b as usize].push(k as u32);
                }
            }
            states.push(st);
        }
        Ok(Self { states })
    }

    /// climate.au_state's index into the states (-1 none) of each point (lat, lon 1-D): outside the box
    /// lat_box x lon_box none; inside, the point's cell (round(lat * grid), round(lon * grid), ties to even) as
    /// climate._au_index places it (pad the boxes' margin, km the coast distance); each cell once.
    #[allow(clippy::too_many_arguments)]
    fn index<'py>(&self, py: Python<'py>, lat: PyReadonlyArray1<f64>, lon: PyReadonlyArray1<f64>, grid: f64,
                  lat_box: (f64, f64), lon_box: (f64, f64), pad: f64, km: f64)
                  -> PyResult<Bound<'py, PyArray1<i64>>> {
        let (la, lo) = (lat.as_array(), lon.as_array());
        if la.len() != lo.len() {
            return Err(PyValueError::new_err("AuStates.index: lat and lon of one length"));
        }
        let (la, lo) = (la.to_vec(), lo.to_vec());
        let out = py.detach(|| {
            let mut cache: HashMap<(i64, i64), i64> = HashMap::new();
            let mut buf = Vec::new();
            la.iter().zip(&lo).map(|(&y, &x)| {
                if !(y >= lat_box.0 && y <= lat_box.1 && x >= lon_box.0 && x <= lon_box.1) {
                    return -1;
                }
                let cell = ((y * grid).round_ties_even() as i64, (x * grid).round_ties_even() as i64);
                *cache.entry(cell).or_insert_with(|| {
                    self.cell(cell.0 as f64 / grid, cell.1 as f64 / grid, pad, km, &mut buf)
                })
            }).collect::<Vec<i64>>()
        });
        Ok(out.into_pyarray(py))
    }
}

impl AuStates {
    fn in_box(&self, s: &State, px: f64, py: f64, pad: f64) -> bool {
        px >= s.bb[0] - pad && px <= s.bb[2] + pad && py >= s.bb[1] - pad && py <= s.bb[3] + pad
    }

    /// climate._au_index for one point (py lat, px lon).
    fn cell(&self, py: f64, px: f64, pad: f64, km: f64, buf: &mut Vec<u32>) -> i64 {
        for (si, s) in self.states.iter().enumerate() {
            if !self.in_box(s, px, py, pad) {
                continue;
            }
            s.near(py, py, buf);
            let mut odd = false;
            for &k in buf.iter() {
                let [x1, y1, x2, y2] = s.seg[k as usize];
                if (y1 > py) != (y2 > py) && px < (x2 - x1) * (py - y1) / (if y2 == y1 { 1.0 } else { y2 - y1 }) + x1 {
                    odd = !odd;
                }
            }
            if odd {
                return si as i64;
            }
        }
        // off every outline: the nearest state's coast within km (a later state only when strictly nearer)
        let cs = py.to_radians().dcos();
        let reach = km / 111.195 * 1.001 + 1e-9; // (latitude alone farther than this: farther than km)
        let (mut best, mut out) = (km, -1i64);
        for (si, s) in self.states.iter().enumerate() {
            if !self.in_box(s, px, py, pad) {
                continue;
            }
            s.near(py - reach, py + reach, buf);
            if buf.is_empty() {
                continue;
            }
            let mut h = f64::INFINITY;
            for &k in buf.iter() {
                let [x1, y1, x2, y2] = s.seg[k as usize];
                let (dx, dy) = ((x2 - x1) * cs, y2 - y1);
                let l = dx * dx + dy * dy;
                let t = (((px - x1) * cs * dx + (py - y1) * dy) / (if l == 0.0 { 1.0 } else { l })).clamp(0.0, 1.0);
                h = h.min(((px - x1) * cs - t * dx).dhypot((py - y1) - t * dy));
            }
            let d = h * 111.195;
            if d < best {
                best = d;
                out = si as i64;
            }
        }
        out
    }
}
