//! Short cycles of a road graph (islands._cycles: roundabouts, teardrop islands): a depth-first walk per edge back to
//! its start, ~1.2 s of Python for 8 of Bratislava's road tiles (2026-10-28).

use std::collections::{HashMap, HashSet};

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

/// Each short cycle of the graph whose edges are (a[k], b[k], length[k]) once, in islands._cycles' order: per start
/// edge k0 (its b back to its a; a closed edge alone), a stack walk taking each node's edges in index order, at most
/// max_edges edges and max_loop m; a cycle seen before (the same set of edges) left out. (off (c + 1), edge (m),
/// forward (m)): cycle i is edge[off[i]..off[i + 1]], each traversed from its start (forward) or its end.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn road_cycles<'py>(py: Python<'py>, a: PyReadonlyArray1<'py, i64>, b: PyReadonlyArray1<'py, i64>,
                        length: PyReadonlyArray1<'py, f64>, max_edges: usize, max_loop: f64)
                        -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<bool>>) {
    let (a, b, length) = (a.as_array(), b.as_array(), length.as_array());
    let (off, edge, fwd) = py.detach(|| {
        let n = a.len();
        let mut adj: HashMap<i64, Vec<usize>> = HashMap::new();
        for k in 0..n {
            adj.entry(a[k]).or_default().push(k);
            adj.entry(b[k]).or_default().push(k);
        }
        let empty: Vec<usize> = Vec::new();
        let mut seen: HashSet<Vec<usize>> = HashSet::new();
        let (mut off, mut edge, mut fwd) = (vec![0i64], Vec::new(), Vec::new());
        for k0 in 0..n {
            let (a0, b0, l0) = (a[k0], b[k0], length[k0]);
            let mut cycles: Vec<Vec<(usize, bool)>> = Vec::new();
            if a0 == b0 {
                cycles.push(vec![(k0, true)]);
            } else {
                let mut stack: Vec<(i64, Vec<(usize, bool)>, f64)> = vec![(b0, vec![(k0, true)], l0)];
                while let Some((nd, path, l)) = stack.pop() {
                    if path.len() > max_edges || l > max_loop {
                        continue;
                    }
                    for &k in adj.get(&nd).unwrap_or(&empty) {
                        if path.iter().any(|&(q, _)| q == k) {
                            continue;
                        }
                        let f = a[k] == nd;
                        let nxt = if f { b[k] } else { a[k] };
                        if nxt == a0 {
                            let mut c = path.clone();
                            c.push((k, f));
                            cycles.push(c);
                        } else if path.len() < max_edges {
                            let mut c = path.clone();
                            c.push((k, f));
                            stack.push((nxt, c, l + length[k]));
                        }
                    }
                }
            }
            for c in cycles {
                let mut key: Vec<usize> = c.iter().map(|&(k, _)| k).collect();
                key.sort_unstable();
                if !seen.insert(key) {
                    continue;
                }
                for (k, f) in c {
                    edge.push(k as i64);
                    fwd.push(f);
                }
                off.push(edge.len() as i64);
            }
        }
        (off, edge, fwd)
    });
    (off.into_pyarray(py), edge.into_pyarray(py), fwd.into_pyarray(py))
}
