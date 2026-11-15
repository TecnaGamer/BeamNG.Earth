//! grading.ease_coarse: road profiles a few metres apart brought together where the elevation data is coarse (GitHub
//! #414, Perm's Solikamskaya Street on Copernicus' 30 m: service roads 7 m beside a primary stood 4-5 m above it, walls
//! across every junction, where the street photos show it all at grade). A lower-ranked road near a higher-ranked one
//! takes the higher one's height (equal ranks meet halfway), less what a real cross slope between their edges explains.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Nearest point of polyline xy[a..b] to (x, y): (distance, height there from z).
fn nearest(xy: &[[f64; 2]], z: &[f64], a: usize, b: usize, x: f64, y: f64) -> (f64, f64) {
    let (mut bd, mut bz) = (f64::INFINITY, 0.0);
    for i in a..b.saturating_sub(1) {
        let (p, q) = (xy[i], xy[i + 1]);
        let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
        let ll = (dx * dx + dy * dy).max(1e-12);
        let t = (((x - p[0]) * dx + (y - p[1]) * dy) / ll).clamp(0.0, 1.0);
        let (nx, ny) = (p[0] + t * dx, p[1] + t * dy);
        let d = ((nx - x).powi(2) + (ny - y).powi(2)).sqrt();
        if d < bd {
            bd = d;
            bz = z[i] + t * (z[i + 1] - z[i]);
        }
    }
    (bd, bz)
}

/// New heights for the plans' vertices. xy (n, 2), z (n): every plan's vertices one after another, plan j's from
/// off[j] to off[j + 1]; rank (plans): its road class rank; half: its carriageway's half width; take: whether it takes
/// part (0: a bridge approach, a plan left as it is). reach (m): how far from the other road's edge it is pulled (a
/// Gaussian weight of the distance past that edge); max_step (m): a bigger difference is a real separation, left
/// alone; max_slope: the cross slope between the two carriageways' edges kept; sigma (m): the change smoothed along
/// the road. Higher ranks first: a lower road follows its neighbour's already eased height. Pure.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn ease_coarse<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    rank: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
    take: PyReadonlyArray1<'py, u8>,
    reach: f64,
    max_step: f64,
    max_slope: f64,
    sigma: f64,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let xa = xy.as_array();
    let n = xa.nrows();
    let pts: Vec<[f64; 2]> = (0..n).map(|i| [xa[[i, 0]], xa[[i, 1]]]).collect();
    let mut zz: Vec<f64> = z.as_array().to_vec();
    let off: Vec<usize> = off.as_array().iter().map(|&v| v.max(0) as usize).collect();
    let rank: Vec<i64> = rank.as_array().to_vec();
    let half: Vec<f64> = half.as_array().to_vec();
    let take: Vec<u8> = take.as_array().to_vec();
    let np = rank.len();
    if off.len() != np + 1 || zz.len() != n || half.len() != np || take.len() != np {
        return Err(pyo3::exceptions::PyValueError::new_err("ease_coarse: array sizes differ"));
    }
    // each plan's box, grown by the reach and its half width
    let mut boxes = vec![[f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY]; np];
    for j in 0..np {
        for p in &pts[off[j]..off[j + 1]] {
            let b = &mut boxes[j];
            b[0] = b[0].min(p[0]);
            b[1] = b[1].min(p[1]);
            b[2] = b[2].max(p[0]);
            b[3] = b[3].max(p[1]);
        }
        let g = reach + half[j];
        boxes[j] = [boxes[j][0] - g, boxes[j][1] - g, boxes[j][2] + g, boxes[j][3] + g];
    }
    let mut order: Vec<usize> = (0..np).collect();
    order.sort_by(|&a, &b| rank[b].cmp(&rank[a]).then(a.cmp(&b)));
    for &j in &order {
        let (a, b) = (off[j], off[j + 1]);
        if take[j] == 0 || b < a + 2 {
            continue;
        }
        let mut dz = vec![0.0f64; b - a];
        let mut any = false;
        for v in a..b {
            let [x, y] = pts[v];
            let (mut bw, mut bdelta) = (0.0f64, 0.0f64);
            for k in 0..np {
                if k == j || take[k] == 0 || rank[k] < rank[j] || off[k + 1] < off[k] + 2 {
                    continue;
                }
                let bx = &boxes[k];
                if x < bx[0] || x > bx[2] || y < bx[1] || y > bx[3] {
                    continue;
                }
                let (d, zq) = nearest(&pts, &zz, off[k], off[k + 1], x, y);
                let past = (d - half[k]).max(0.0);
                if past > reach {
                    continue;
                }
                let diff = zq - zz[v];
                if diff.abs() > max_step {
                    continue;
                }
                let gap = (d - half[k] - half[j]).max(0.0);
                let excess = (diff.abs() - max_slope * gap).max(0.0) * diff.signum();
                let share = if rank[k] > rank[j] { 1.0 } else { 0.5 };
                let w = share * (-(past / reach).powi(2)).dexp();
                if w > bw {
                    bw = w;
                    bdelta = excess * w;
                }
            }
            if bdelta != 0.0 {
                dz[v - a] = bdelta;
                any = true;
            }
        }
        if !any {
            continue;
        }
        // smoothed along the road (a Gaussian over arc length): no step where the neighbour ends
        let mut s = vec![0.0f64; b - a];
        for i in 1..(b - a) {
            let (p, q) = (pts[a + i - 1], pts[a + i]);
            s[i] = s[i - 1] + ((q[0] - p[0]).powi(2) + (q[1] - p[1]).powi(2)).sqrt();
        }
        let sg = sigma.max(1e-6);
        for i in 0..(b - a) {
            let (mut num, mut den) = (0.0, 0.0);
            for k in 0..(b - a) {
                let u = (s[k] - s[i]) / sg;
                if u.abs() > 3.0 {
                    continue;
                }
                let w = (-0.5 * u * u).dexp();
                num += w * dz[k];
                den += w;
            }
            zz[a + i] += if den > 0.0 { num / den } else { 0.0 };
        }
    }
    Ok(zz.into_pyarray(py))
}
