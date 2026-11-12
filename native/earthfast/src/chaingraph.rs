//! roads._chain_graph (version 242): the nodes and edges of a set of lines, their ends joined as roads._EndJoin does.
//! The Python joiner (a dict lookup per grid cell for every end, a numpy hypot / cumsum per line) was ~3 % of a fresh
//! cell's plans: the rails' stubs and underpasses, the bridge chains, the plans' ends ask for it per window.

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

/// roads._EndJoin(tol): the first point seen names a node; a later one joins the node of its nearest point within tol
/// (squared distances; on a tie the point seen first), and is one of that node's points after unless it lies on it
/// (1e-12); the 3 x 3 grid cells of tol round it looked up, in _EndJoin's order.
struct EndJoin {
    tol: f64,
    grid: HashMap<(i64, i64), Vec<usize>>,
    pts: Vec<(f64, f64)>,
    node_of: Vec<usize>,
    n: usize,
}

impl EndJoin {
    fn new(tol: f64) -> EndJoin {
        EndJoin { tol, grid: HashMap::new(), pts: Vec::new(), node_of: Vec::new(), n: 0 }
    }

    /// (node, new: whether the point named a new node)
    fn join(&mut self, x: f64, y: f64) -> (usize, bool) {
        let (gx, gy) = ((x / self.tol).floor() as i64, (y / self.tol).floor() as i64);
        let (mut best, mut bd): (Option<usize>, f64) = (None, self.tol * self.tol);
        for dx in -1..=1 {
            for dy in -1..=1 {
                if let Some(v) = self.grid.get(&(gx + dx, gy + dy)) {
                    for &i in v {
                        let q = self.pts[i];
                        let d = (q.0 - x) * (q.0 - x) + (q.1 - y) * (q.1 - y);
                        if d < bd || (d == bd && best.is_some_and(|b| i < b)) {
                            best = Some(i);
                            bd = d;
                        }
                    }
                }
            }
        }
        if let Some(b) = best {
            let nd = self.node_of[b];
            if bd > 1e-12 {
                self.pts.push((x, y));
                self.node_of.push(nd);
                self.grid.entry((gx, gy)).or_default().push(self.pts.len() - 1);
            }
            return (nd, false);
        }
        self.pts.push((x, y));
        self.node_of.push(self.n);
        self.grid.entry((gx, gy)).or_default().push(self.pts.len() - 1);
        self.n += 1;
        (self.n - 1, true)
    }
}

/// roads._chain_graph of lines [(k, 2+)] (k >= 1): (nodes (N, 2) in the order they are named, start node (E,), end node
/// (E,), the lines' arc lengths end to end (numpy's: hypot of each step, summed in order, from 0), their offsets (E + 1,)).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn chain_graph<'py>(py: Python<'py>, lines: Vec<PyReadonlyArray2<'py, f64>>, tol: f64)
                        -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                     Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>)> {
    let views: Vec<_> = lines.iter().map(|l| l.as_array()).collect();
    if views.iter().any(|v| v.nrows() == 0 || v.ncols() < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("chain_graph: a line of no points"));
    }
    let mut j = EndJoin::new(tol);
    let mut nodes: Vec<f64> = Vec::new();
    let (mut a, mut b) = (Vec::with_capacity(views.len()), Vec::with_capacity(views.len()));
    let (mut s, mut off) = (Vec::new(), vec![0i64]);
    for v in &views {
        let k = v.nrows();
        let mut acc = 0.0f64;
        s.push(0.0);
        for i in 1..k {
            acc += (v[[i, 0]] - v[[i - 1, 0]]).hypot(v[[i, 1]] - v[[i - 1, 1]]);
            s.push(acc);
        }
        off.push(s.len() as i64);
        for (e, out) in [(0, &mut a), (k - 1, &mut b)] {
            let (x, y) = (v[[e, 0]], v[[e, 1]]);
            let (nd, new) = j.join(x, y);
            if new {
                nodes.push(x);
                nodes.push(y);
            }
            out.push(nd as i64);
        }
    }
    let nodes = numpy::ndarray::Array2::from_shape_vec((nodes.len() / 2, 2), nodes).expect("nodes");
    Ok((nodes.into_pyarray(py), a.into_pyarray(py), b.into_pyarray(py), s.into_pyarray(py), off.into_pyarray(py)))
}
