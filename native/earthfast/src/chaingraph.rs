//! roads._chain_graph (version 242): the nodes and edges of a set of lines, their ends joined as roads._EndJoin does.
//! The Python joiner (a dict lookup per grid cell for every end, a numpy hypot / cumsum per line) was ~3 % of a fresh
//! cell's plans: the rails' stubs and underpasses, the bridge chains, the plans' ends ask for it per window.
use crate::dm::Dm;

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

/// roads._EndJoin(tol): the first point seen names a node; a later one joins the node of its nearest point within tol
/// (squared distances; on a tie the point seen first), and is one of that node's points after unless it lies on it
/// (1e-12); the 3 x 3 grid cells of tol round it looked up, in _EndJoin's order.
pub(crate) struct EndJoin {
    tol: f64,
    grid: HashMap<(i64, i64), Vec<usize>>,
    pts: Vec<(f64, f64)>,
    node_of: Vec<usize>,
    n: usize,
}

impl EndJoin {
    pub(crate) fn new(tol: f64) -> EndJoin {
        EndJoin { tol, grid: HashMap::new(), pts: Vec::new(), node_of: Vec::new(), n: 0 }
    }

    /// (node, new: whether the point named a new node)
    pub(crate) fn join(&mut self, x: f64, y: f64) -> (usize, bool) {
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
            acc += (v[[i, 0]] - v[[i - 1, 0]]).dhypot(v[[i, 1]] - v[[i - 1, 1]]);
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

type PathEdge = (usize, bool); // original edge index and whether it is walked backwards

struct DeckTree {
    distance: Vec<f64>,
    parent: Vec<Option<(usize, PathEdge)>>,
    through: Vec<bool>,
    top: Vec<f64>,
    discovered: Vec<usize>,
}

impl DeckTree {
    fn path(&self, mut end: usize) -> Vec<PathEdge> {
        let mut path = Vec::new();
        while let Some((u, edge)) = self.parent[end] {
            path.push(edge);
            end = u;
        }
        path.reverse();
        path
    }
}

struct DeckGraph {
    edges: Vec<(usize, usize, f64, f64)>,
    incident: Vec<Vec<(usize, usize, bool)>>,
    tangents: Option<Vec<[[f64; 2]; 2]>>,
    turn_cos: f64,
}

impl DeckGraph {
    // A tree over the unused edges. Stop on arrival at a terminal, as the
    // branch walk does when it meets an abutment or an already fitted joint.
    fn tree(&self, source: usize, used: &[bool], terminal: &[bool]) -> DeckTree {
        use std::cmp::Reverse;
        use std::collections::BinaryHeap;
        let n = self.incident.len();
        let mut tree = DeckTree {
            distance: vec![f64::INFINITY; n], parent: vec![None; n],
            through: vec![true; n], top: vec![f64::NEG_INFINITY; n], discovered: Vec::new(),
        };
        tree.distance[source] = 0.0;
        // Nonnegative distances ordered by their bits, then by node, like heapq.
        let mut queue = BinaryHeap::from([Reverse((0u64, source))]);
        while let Some(Reverse((bits, u))) = queue.pop() {
            let d = f64::from_bits(bits);
            if d > tree.distance[u] || (u != source && terminal[u]) {
                continue;
            }
            for &(k, v, rev) in &self.incident[u] {
                if used[k] { continue; }
                let next = d + self.edges[k].2;
                if next < tree.distance[v] {
                    if tree.parent[v].is_none() { tree.discovered.push(v); }
                    tree.distance[v] = next;
                    tree.parent[v] = Some((u, (k, rev)));
                    tree.top[v] = tree.top[u].max(self.edges[k].3);
                    tree.through[v] = tree.through[u];
                    if let (true, Some(t), Some((_, (prev, prev_rev)))) =
                        (tree.through[u], &self.tangents, tree.parent[u]) {
                        // Both tangents point into their edge from this joint.
                        let incoming = t[prev][usize::from(!prev_rev)];
                        let outgoing = t[k][usize::from(rev)];
                        let dot = crate::dmmatmul::dot1(&[-incoming[0], -incoming[1]], &outgoing);
                        tree.through[v] = !(dot < self.turn_cos);
                    }
                    queue.push(Reverse((next.to_bits(), v)));
                }
            }
        }
        tree
    }
}

/// Schedule the through route and every branch before requesting terrain. A
/// component with edges left over always takes the ground-envelope fallback.
#[pyfunction]
pub fn deck_paths(
    nodes: usize, edges: Vec<(usize, usize, f64, f64)>,
    tangents: Option<Vec<[[f64; 2]; 2]>>, turn_cos: f64,
) -> PyResult<Vec<Vec<PathEdge>>> {
    use pyo3::exceptions::PyValueError;
    if tangents.as_ref().is_some_and(|t| t.len() != edges.len())
        || edges.iter().any(|&(a, b, length, _)| a >= nodes || b >= nodes || length < 0.0) {
        return Err(PyValueError::new_err("deck_paths: invalid edges or tangents"));
    }
    let mut incident = vec![Vec::new(); nodes];
    for (k, &(a, b, _, _)) in edges.iter().enumerate() {
        incident[a].push((k, b, false));
        incident[b].push((k, a, true));
    }
    let mut terminal: Vec<_> = incident.iter().map(|edges| edges.len() == 1).collect();
    let leaves: Vec<_> = (0..nodes).filter(|&i| terminal[i]).collect();
    let mut used = vec![false; edges.len()];
    let graph = DeckGraph { edges, incident, tangents, turn_cos };
    let mut best = None;
    let mut path = Vec::new();
    // A leaf cannot be an intermediate node. One shortest-path tree gives
    // the original path, clearance and turn rank for every destination leaf.
    for (i, &source) in leaves.iter().enumerate().take(leaves.len().saturating_sub(1)) {
        let tree = graph.tree(source, &used, &terminal);
        let mut end = None;
        for &v in &leaves[i + 1..] {
            if tree.parent[v].is_none() { continue; }
            let top = tree.top[v];
            let rank = (tree.through[v], if top.is_finite() { top } else { -1e9 }, tree.distance[v]);
            if best.is_none_or(|old| rank > old) {
                best = Some(rank);
                end = Some(v);
            }
        }
        if let Some(v) = end { path = tree.path(v); }
    }
    let mut paths = Vec::new();
    let mut seen = vec![false; nodes];
    let mut solved = Vec::new(); // first visit order, like record()'s node dictionary
    while !path.is_empty() {
        for &(k, rev) in &path {
            used[k] = true;
            let (a, b, _, _) = graph.edges[k];
            for u in if rev { [b, a] } else { [a, b] } {
                if !seen[u] {
                    seen[u] = true;
                    terminal[u] = true;
                    solved.push(u);
                }
            }
        }
        paths.push(path);
        let Some(&start) = solved.iter().find(|&&u| graph.incident[u].iter().any(|&(k, _, _)| !used[k])) else {
            break;
        };
        let tree = graph.tree(start, &used, &terminal);
        let mut far = None;
        for &v in &tree.discovered {
            if terminal[v] && far.is_none_or(|u: usize| tree.distance[v] > tree.distance[u]) {
                far = Some(v);
            }
        }
        // This residual component has no exit except its start. No later
        // branch can enter it, so the original fit would leave edges unused.
        let Some(v) = far else { return Ok(Vec::new()); };
        path = tree.path(v);
    }
    Ok(if used.iter().all(|&u| u) { paths } else { Vec::new() })
}

/// Ground-envelope constraints for every deck vertex, in crossing order. Keep
/// one distance tree at a time. A later at-grade crossing reads the cap left by
/// earlier crossings, including ones along other parts of the chain.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn deck_constraints<'py>(
    py: Python<'py>, node_count: usize, ends: Vec<(usize, usize)>,
    arcs: Vec<numpy::PyReadonlyArray1<'py, f64>>, abutments: Vec<(usize, f64)>,
    levels: Vec<f64>, crossings: Vec<(usize, f64, f64, f64, bool, Option<f64>)>,
    n_road: usize, params: (f64, f64, f64, f64, f64, f64),
) -> PyResult<Vec<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)>> {
    use crate::railprof::{dijkstra, np_max, np_min, py_max};
    use pyo3::exceptions::PyValueError;
    let (max_grade, steep_grade, grade, road_reach, at_reach, at_height) = params;
    let arcs: Vec<&[f64]> = arcs.iter().map(|a| a.as_slice()).collect::<Result<_, _>>()?;
    if ends.len() != arcs.len() || levels.len() != ends.len() || arcs.iter().any(|s| s.is_empty())
        || ends.iter().any(|&(a, b)| a >= node_count || b >= node_count)
        || abutments.iter().any(|&(a, _)| a >= node_count)
        || crossings.iter().any(|c| c.0 >= ends.len()) {
        return Err(PyValueError::new_err("deck_constraints: invalid graph"));
    }
    let mut adj = vec![Vec::new(); node_count];
    let fill = |value| arcs.iter().map(|s| vec![value; s.len()]).collect::<Vec<_>>();
    let mut cap = fill(f64::INFINITY);
    let mut dab = fill(f64::INFINITY);
    let mut steep = fill(f64::INFINITY);
    let mut req = fill(f64::NEG_INFINITY);
    for (&(a, b), s) in ends.iter().zip(&arcs) {
        let len = s[s.len() - 1];
        adj[a].push((b, len));
        adj[b].push((a, len));
    }
    for (node, z) in abutments {
        let d = dijkstra(&adj, &[(node, 0.0)]);
        for (k, (&(a, b), &s)) in ends.iter().zip(&arcs).enumerate() {
            let len = s[s.len() - 1];
            for (j, &arc) in s.iter().enumerate() {
                let distance = np_min(d[a] + arc, d[b] + (len - arc));
                cap[k][j] = np_min(cap[k][j], z + max_grade * distance);
                dab[k][j] = np_min(dab[k][j], distance);
                steep[k][j] = np_min(steep[k][j], z + steep_grade * distance);
            }
        }
    }
    for (i, (part, at, z, clearance, road, min_level)) in crossings.into_iter().enumerate() {
        let (a, b) = ends[part];
        let s = arcs[part];
        let back = s[s.len() - 1] - at;
        // The second seed replaces the first in Python's dict for a self-loop.
        let d = if a == b { dijkstra(&adj, &[(b, back)]) } else { dijkstra(&adj, &[(a, at), (b, back)]) };
        let flat = i < n_road && road
            && crate::dm::interp(&[at], s, &dab[part], None, None)[0] <= at_reach
            && crate::dm::interp(&[at], s, &cap[part], None, None)[0] < z + at_height;
        let reach = if road { road_reach } else { 0.0 };
        let top = z + clearance;
        for (k, (&(a, b), &es)) in ends.iter().zip(&arcs).enumerate() {
            if !flat && min_level.is_some_and(|lev| !(levels[k] >= lev)) { continue; }
            let len = es[es.len() - 1];
            for (j, &arc) in es.iter().enumerate() {
                let mut dk = np_min(d[a] + arc, d[b] + (len - arc));
                if k == part { dk = np_min(dk, (arc - at).abs()); }
                if flat {
                    cap[k][j] = np_min(cap[k][j], z + max_grade * dk);
                } else {
                    req[k][j] = np_max(req[k][j], top - grade * np_max(dk - reach, 0.0));
                }
            }
            if !flat && k == part {
                // Bracket all three corridor stations just as the mesh interpolates them.
                for point in [at - reach, at, at + reach] {
                    if !(0.0 <= point && point <= len) { continue; }
                    let j = es.partition_point(|&r| r < point);
                    if j > 0 && (j == es.len() || (es[j] - point).abs() >= 1e-8) {
                        req[k][j - 1] = py_max(req[k][j - 1], top);
                    }
                    if j < es.len() { req[k][j] = py_max(req[k][j], top); }
                }
            }
        }
    }
    Ok(cap.into_iter().zip(steep).zip(req).map(|((c, s), r)|
        (c.into_pyarray(py), s.into_pyarray(py), r.into_pyarray(py))).collect())
}
