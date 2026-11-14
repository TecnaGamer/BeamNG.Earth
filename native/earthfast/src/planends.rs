//! grading's bridge-end queries, a cell's plan ends at a time (grading._Planner._ends_near and the bridge-chain
//! helpers). Every road end planned in a cell asked, one at a time in Python: the cells round it, their bridge ends
//! concatenated, rounded to the millimetre and sorted unique (numpy), then the ones at a distance band from it. A cold
//! Fells Point planning spent ~4 % of all its samples there (_junction_lift, 2026-11-10), most ends with none near.
//! The same numbers here: numpy's round (x * 1000, rint, / 1000), its row order (x, then y) and the C library's hypot
//! (numpy's np.hypot), each query's own cells (the caller gives each point's cell box, as cells_for_points makes it).

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// np.round(x, 3): x * 10**3, rint, / 10**3 (numpy's PyArray_Round for a float array).
#[inline]
fn round3(x: f64) -> f64 {
    (x * 1000.0).round_ties_even() / 1000.0
}

/// Per query point (qxy (n, 2)) the bridge ends of its cells (columns ix0..=ix1 (mod nmod), rows iy0..=iy1; the cells'
/// ends: cell (cx[c], cy[c]) holds ends[off[c]..off[c + 1]], ok[c] false: that cell could not be read) at
/// rmin < d <= rmax from it, rounded to the mm first when round (as np.round(.., 3)) and each once in (x, y) order
/// (np.unique(.., axis=0)), leaving out those within rother of the query's other point (oxy (n, 2); a NaN row: none).
/// Returns (status (n,): 1 read, 0 a cell of its own failed; offsets (n + 1,); candidates (k, 2)).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn plan_ends_near<'py>(py: Python<'py>, qxy: PyReadonlyArray2<'py, f64>, ix0: PyReadonlyArray1<'py, i64>,
                           ix1: PyReadonlyArray1<'py, i64>, iy0: PyReadonlyArray1<'py, i64>,
                           iy1: PyReadonlyArray1<'py, i64>, oxy: PyReadonlyArray2<'py, f64>,
                           cx: PyReadonlyArray1<'py, i64>, cy: PyReadonlyArray1<'py, i64>,
                           ok: PyReadonlyArray1<'py, bool>, off: PyReadonlyArray1<'py, i64>,
                           ends: PyReadonlyArray2<'py, f64>, nmod: i64, rmin: f64, rmax: f64, rother: f64,
                           round: bool)
                           -> PyResult<(Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>)> {
    let (q, x0, x1, y0, y1, o) = (qxy.as_array(), ix0.as_array(), ix1.as_array(), iy0.as_array(), iy1.as_array(),
                                  oxy.as_array());
    let (cx, cy, ok, off, e) = (cx.as_array(), cy.as_array(), ok.as_array(), off.as_array(), ends.as_array());
    let n = q.nrows();
    if x0.len() != n || x1.len() != n || y0.len() != n || y1.len() != n || (o.nrows() != 0 && o.nrows() != n)
        || cy.len() != cx.len() || ok.len() != cx.len() || off.len() != cx.len() + 1 || nmod <= 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("plan_ends_near: mismatched arrays"));
    }
    let mut cell: HashMap<(i64, i64), usize> = HashMap::with_capacity(cx.len());
    for c in 0..cx.len() {
        cell.insert((cx[c], cy[c]), c);
    }
    let ce: Vec<[f64; 2]> = (0..e.nrows()).map(|i| {
        let (a, b) = (e[[i, 0]], e[[i, 1]]);
        if round { [round3(a), round3(b)] } else { [a, b] }
    }).collect();
    let has_o = o.nrows() == n;
    let mut status = vec![1u8; n];
    let mut outoff = Vec::with_capacity(n + 1);
    outoff.push(0i64);
    let mut out: Vec<[f64; 2]> = Vec::new();
    let mut got: Vec<[f64; 2]> = Vec::new();
    for i in 0..n {
        let (px, py_) = (q[[i, 0]], q[[i, 1]]);
        got.clear();
        let mut bad = false;
        'cells: for iy in y0[i]..=y1[i] {
            for ix in x0[i]..=x1[i] {
                let k = (ix.rem_euclid(nmod), iy);
                let c = match cell.get(&k) {
                    Some(&c) => c,
                    None => { bad = true; break 'cells; }
                };
                if !ok[c] {
                    bad = true;
                    break 'cells;
                }
                for p in &ce[off[c] as usize..off[c + 1] as usize] {
                    let d = (p[0] - px).hypot(p[1] - py_);
                    if !(d > rmin && d <= rmax) {
                        continue;
                    }
                    if has_o {
                        let (ox, oy) = (o[[i, 0]], o[[i, 1]]);
                        if !ox.is_nan() && (ox - p[0]).hypot(oy - p[1]) <= rother {
                            continue;
                        }
                    }
                    got.push(*p);
                }
            }
        }
        if bad {
            status[i] = 0;
        } else {
            // (np.unique(axis=0): the rows sorted field by field, equal rows once)
            got.sort_by(|a, b| a[0].partial_cmp(&b[0]).unwrap_or(std::cmp::Ordering::Equal)
                .then(a[1].partial_cmp(&b[1]).unwrap_or(std::cmp::Ordering::Equal)));
            got.dedup_by(|a, b| a[0] == b[0] && a[1] == b[1]);
            out.extend_from_slice(&got);
        }
        outoff.push(out.len() as i64);
    }
    let flat: Vec<f64> = out.iter().flat_map(|p| [p[0], p[1]]).collect();
    let arr = numpy::ndarray::Array2::from_shape_vec((out.len(), 2), flat)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((status.into_pyarray(py), outoff.into_pyarray(py), arr.into_pyarray(py)))
}

/// A line's first two columns as points.
fn pts2(a: &numpy::ndarray::ArrayView2<f64>) -> Vec<[f64; 2]> {
    (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect()
}

/// np.array_equal of two lines: their first two columns (cols2), or the whole arrays (shape and every value).
fn same(a: &numpy::ndarray::ArrayView2<f64>, b: &numpy::ndarray::ArrayView2<f64>, cols2: bool) -> bool {
    if a.nrows() != b.nrows() {
        return false;
    }
    if cols2 {
        if a.ncols() < 2 || b.ncols() < 2 {
            return a.ncols().min(2) == b.ncols().min(2) && a.iter().zip(b.iter()).all(|(x, y)| x == y);
        }
        (0..a.nrows()).all(|i| a[[i, 0]] == b[[i, 0]] && a[[i, 1]] == b[[i, 1]])
    } else {
        a.ncols() == b.ncols() && a.iter().zip(b.iter()).all(|(x, y)| x == y)
    }
}

/// grading.bridge_lifts' joined_at / bridge_chain_ends_free, every asked point at once: per end point (ends (q, 2)),
/// whether another bridge part (bridges, each (k, 2+); one equal to own, its first two columns (cols2) or the whole
/// array, is own itself) has its first or last point within tol (min of the two hypots < tol).
#[pyfunction]
pub fn plan_bridges_joined<'py>(bridges: Vec<PyReadonlyArray2<'py, f64>>, own: PyReadonlyArray2<'py, f64>,
                                ends: PyReadonlyArray2<'py, f64>, tol: f64, cols2: bool) -> Vec<bool> {
    let own = own.as_array();
    let e = ends.as_array();
    let tips: Vec<Option<([f64; 2], [f64; 2])>> = bridges.iter().map(|b| {
        let b = b.as_array();
        if b.nrows() == 0 || b.ncols() < 2 || same(&b, &own, cols2) {
            None
        } else {
            let l = b.nrows() - 1;
            Some(([b[[0, 0]], b[[0, 1]]], [b[[l, 0]], b[[l, 1]]]))
        }
    }).collect();
    (0..e.nrows()).map(|i| {
        let (x, y) = (e[[i, 0]], e[[i, 1]]);
        tips.iter().flatten().any(|(f, l)| (f[0] - x).hypot(f[1] - y).min((l[0] - x).hypot(l[1] - y)) < tol)
    }).collect()
}

/// grading.bridge_chain: the indices (in bridges) of the parts joined to line through their ends (within tol), line
/// itself (a part equal to it in its first two columns) left out, in the order the frontier search finds them.
#[pyfunction]
pub fn plan_bridge_chain<'py>(bridges: Vec<PyReadonlyArray2<'py, f64>>, line: PyReadonlyArray2<'py, f64>, tol: f64)
                              -> Vec<usize> {
    let line = line.as_array();
    let mut idx = Vec::new();
    let mut first = Vec::new();
    let mut last = Vec::new();
    for (j, b) in bridges.iter().enumerate() {
        let b = b.as_array();
        if same(&b, &line, true) {
            continue;
        }
        let p = pts2(&b);
        if p.is_empty() {
            continue;
        }
        idx.push(j);
        first.push(p[0]);
        last.push(p[p.len() - 1]);
    }
    let mut out = Vec::new();
    if idx.is_empty() || line.nrows() == 0 {
        return out;
    }
    let l = line.nrows() - 1;
    let mut frontier = vec![[line[[0, 0]], line[[0, 1]]], [line[[l, 0]], line[[l, 1]]]];
    let mut used = vec![false; idx.len()];
    while let Some(e) = frontier.pop() {
        for k in 0..idx.len() {
            // (np.minimum of the two hypots: a NaN either way is NaN, never < tol)
            let a = (first[k][0] - e[0]).hypot(first[k][1] - e[1]);
            let b = (last[k][0] - e[0]).hypot(last[k][1] - e[1]);
            let d = if a.is_nan() || b.is_nan() { f64::NAN } else { a.min(b) };
            if d < tol && !used[k] {
                used[k] = true;
                out.push(idx[k]);
                frontier.push(first[k]);
                frontier.push(last[k]);
            }
        }
    }
    out
}
