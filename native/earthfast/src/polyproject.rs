//! rampmerges.project: each point's nearest spot on a polyline (in xy) — its station along the line (extrapolated past
//! the ends), the spot, the segment's unit tangent, the signed left offset and the xy distance. The numpy version made
//! points x segments matrices for every call (6,000 calls per 9 road tiles at White Plains, ~6 % of the road build,
//! 2026-11-07). Same arithmetic in the same order (first minimum, NaN first as numpy's argmin), so bit-identical.
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

pub struct Projected {
    pub station: Vec<f64>,
    pub near: Vec<f64>, // (m, line columns)
    pub tangent: Vec<f64>,
    pub offset: Vec<f64>,
    pub dist: Vec<f64>,
}

/// line: n x lc (lc >= 2, n >= 2), points: m x pc (pc >= 2).
pub fn project(line: &[f64], lc: usize, n: usize, pts: &[f64], pc: usize, m: usize) -> Projected {
    let ns = n - 1;
    let mut dx = vec![0.0; ns];
    let mut dy = vec![0.0; ns];
    let mut len = vec![0.0; ns];
    let mut arc = vec![0.0; n];
    for s in 0..ns {
        dx[s] = line[(s + 1) * lc] - line[s * lc];
        dy[s] = line[(s + 1) * lc + 1] - line[s * lc + 1];
        let l = (dx[s] * dx[s] + dy[s] * dy[s]).sqrt();
        arc[s + 1] = arc[s] + l;
        len[s] = l.max(1e-9);
    }
    let mut out = Projected {
        station: Vec::with_capacity(m),
        near: Vec::with_capacity(m * lc),
        tangent: Vec::with_capacity(m * 2),
        offset: Vec::with_capacity(m),
        dist: Vec::with_capacity(m),
    };
    for i in 0..m {
        let (px, py) = (pts[i * pc], pts[i * pc + 1]);
        let (mut bk, mut bd, mut bt, mut btc) = (0usize, f64::INFINITY, 0.0, 0.0);
        let mut found = false;
        for s in 0..ns {
            let (ax, ay) = (line[s * lc], line[s * lc + 1]);
            let t = ((px - ax) * dx[s] + (py - ay) * dy[s]) / (len[s] * len[s]);
            let tc = t.clamp(0.0, 1.0);
            let tc = if t.is_nan() { t } else { tc };
            let qx = ax + tc * dx[s];
            let qy = ay + tc * dy[s];
            let ex = px - qx;
            let ey = py - qy;
            let d = ex * ex + ey * ey;
            // numpy argmin: the first minimum, a NaN the minimum at once
            let better = if !found { true } else if bd.is_nan() { false } else { d.is_nan() || d < bd };
            if better {
                bk = s;
                bd = d;
                bt = t;
                btc = tc;
                found = true;
            }
        }
        let k = bk;
        for c in 0..lc {
            let a = line[k * lc + c];
            out.near.push(a + btc * (line[(k + 1) * lc + c] - a));
        }
        let (tx, ty) = (dx[k] / len[k], dy[k] / len[k]);
        out.tangent.push(tx);
        out.tangent.push(ty);
        let fraction = if (k == 0 && bt < 0.0) || (k == ns - 1 && bt > 1.0) { bt } else { btc };
        out.station.push(arc[k] + fraction * len[k]);
        let (nx, ny) = (out.near[i * lc], out.near[i * lc + 1]);
        out.offset.push((px - nx) * -ty + (py - ny) * tx);
        out.dist.push(bd.sqrt());
    }
    out
}

/// rampmerges.project in Rust: (station (m,), near (m, line cols), tangent (m, 2), offset (m,), distance (m,)).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn polyline_project<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, points: PyReadonlyArray2<'py, f64>)
    -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                 Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let la = line.as_array();
    let pa = points.as_array();
    let (n, lc) = (la.nrows(), la.ncols());
    let (m, pc) = (pa.nrows(), pa.ncols());
    if n < 2 || lc < 2 || pc < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("polyline_project: a line of 2 points or more, xy at least"));
    }
    let lv: Vec<f64> = la.iter().copied().collect();
    let pv: Vec<f64> = pa.iter().copied().collect();
    let r = py.detach(|| project(&lv, lc, n, &pv, pc, m));
    let near = numpy::ndarray::Array2::from_shape_vec((m, lc), r.near).unwrap();
    let tan = numpy::ndarray::Array2::from_shape_vec((m, 2), r.tangent).unwrap();
    Ok((r.station.into_pyarray(py), near.into_pyarray(py), tan.into_pyarray(py), r.offset.into_pyarray(py),
        r.dist.into_pyarray(py)))
}
