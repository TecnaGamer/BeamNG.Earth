//! Stitch clipped local terrain to the rendered rims of neighbouring Mercator tiles.
use crate::{dm::Dm, ear_clip};
use numpy::{ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::{BTreeMap, BTreeSet};

type Point = [f64; 3];
// Positions, normals, UV, and planar UV share one index throughout subdivision.
type Vertex = [f64; 10];
type Face = [usize; 3];
const EDGE_EPS: f64 = 1e-6;

#[derive(Clone, Copy)]
struct Knot {
    along: f64,
    point: Point,
}

struct Edge {
    start: [f64; 2],
    end: [f64; 2],
    spacing: f64,
    knots: Vec<Knot>,
}

struct Profile {
    start: [f64; 2],
    dir: [f64; 2],
    local: Vec<Knot>,
    far: Vec<Knot>,
    spacing: f64,
}

struct Mesh {
    vertices: Vec<Vertex>,
    faces: Vec<Face>,
    origin: Point,
}

impl Mesh {
    fn world(&self, i: usize) -> Point {
        std::array::from_fn(|j| self.vertices[i][j] + self.origin[j])
    }

    fn compact(&mut self) {
        let used: BTreeSet<_> = self.faces.iter().flatten().copied().collect();
        let mut map = vec![0; self.vertices.len()];
        self.vertices = used.into_iter().enumerate().map(|(i, old)| {
            map[old] = i;
            self.vertices[old]
        }).collect();
        for i in self.faces.iter_mut().flatten() {
            *i = map[*i];
        }
    }

    /// Remove the old skirt and put the global breakpoints into the cut faces.
    fn split_edge(&mut self, along: &[f64], on_edge: &[bool], far: &[Knot]) {
        let mut kept = Vec::new();
        let mut added = Vec::new();
        let mut edges: BTreeMap<(usize, usize), Vec<usize>> = BTreeMap::new();
        for face in std::mem::take(&mut self.faces) {
            let count = face.iter().filter(|&&v| on_edge[v]).count();
            if count == 3 {
                continue;
            }
            if count < 2 {
                kept.push(face);
                continue;
            }
            let mut ring = Vec::new();
            for i in 0..3 {
                let (a, b) = (face[i], face[(i + 1) % 3]);
                ring.push(a);
                if !on_edge[a] || !on_edge[b] || (along[a] - along[b]).abs() <= EDGE_EPS {
                    continue;
                }
                let (u, v) = (a.min(b), a.max(b));
                let indices = edges.entry((u, v)).or_insert_with(|| {
                    let lo = along[u].min(along[v]) + EDGE_EPS;
                    let hi = along[u].max(along[v]) - EDGE_EPS;
                    let first = far.partition_point(|k| k.along < lo);
                    let last = far.partition_point(|k| k.along < hi);
                    let mut ids = Vec::new();
                    for k in &far[first..last.max(first)] {
                        let t = (k.along - along[u]) / (along[v] - along[u]);
                        ids.push(self.vertices.len());
                        self.vertices.push(lerp(self.vertices[u], self.vertices[v], t));
                    }
                    if along[u] > along[v] {
                        ids.reverse();
                    }
                    ids
                });
                if a == u {
                    ring.extend(indices.iter().copied());
                } else {
                    ring.extend(indices.iter().rev().copied());
                }
            }
            if ring.len() == 3 {
                kept.push(face);
            } else {
                let centre = self.vertices.len();
                let [a, b, c] = face.map(|i| self.vertices[i]);
                self.vertices.push(std::array::from_fn(|j| ((a[j] + b[j]) + c[j]) / 3.));
                for i in 0..ring.len() {
                    added.push([centre, ring[i], ring[(i + 1) % ring.len()]]);
                }
            }
        }
        kept.extend(added);
        self.faces = kept;
        self.compact();
    }
}

fn lerp<const N: usize>(a: [f64; N], b: [f64; N], t: f64) -> [f64; N] {
    std::array::from_fn(|i| a[i] + t * (b[i] - a[i]))
}

fn sample(knots: &[Knot], at: f64, extrapolate: bool) -> Point {
    let i = knots.partition_point(|k| k.along < at).saturating_sub(1).min(knots.len() - 2);
    let (a, b) = (knots[i], knots[i + 1]);
    let t = (at - a.along) / (b.along - a.along);
    lerp(a.point, b.point, if extrapolate { t } else { t.clamp(0., 1.) })
}

fn coordinates(start: [f64; 2], dir: [f64; 2], p: Point) -> (f64, f64) {
    let (x, y) = (p[0] - start[0], p[1] - start[1]);
    (x * dir[0] + y * dir[1], x * -dir[1] + y * dir[0])
}

fn prepare(mesh: &mut Mesh, edge: Edge) -> Option<Profile> {
    let [dx, dy] = std::array::from_fn(|i| edge.end[i] - edge.start[i]);
    let length = dx.dhypot(dy);
    if length <= EDGE_EPS {
        return None;
    }
    let dir = [dx / length, dy / length];
    let mut along = Vec::new();
    let mut on = Vec::new();
    let mut local = Vec::new();
    for i in 0..mesh.vertices.len() {
        let p = mesh.world(i);
        let (a, d) = coordinates(edge.start, dir, p);
        let is_edge = d.abs() < EDGE_EPS;
        along.push(a);
        on.push(is_edge);
        if is_edge {
            // Collapse repeated stations from both sides of a cut. The upper vertex is the surface.
            local.push(Knot { along: (a * 1e7).round_ties_even() / 1e7, point: p });
        }
    }
    local.sort_by(|a, b| a.along.partial_cmp(&b.along).unwrap().then_with(|| b.point[2].total_cmp(&a.point[2])));
    local.dedup_by(|b, a| a.along == b.along);
    if local.len() < 2 {
        return None;
    }
    let far: Vec<_> = edge.knots.into_iter().map(|k| Knot { along: k.along * length, ..k }).collect();
    mesh.split_edge(&along, &on, &far);
    Some(Profile { start: edge.start, dir, local, far, spacing: edge.spacing })
}

/// Trim the two overlapping tips to their first crossing, measured outwards from the shared corner.
fn trim_rims(outgoing: &mut Edge, incoming: &mut Edge) {
    let a = &outgoing.knots;
    let b: Vec<_> = incoming.knots.iter().rev().map(|k| Knot { along: 1. - k.along, ..*k }).collect();
    let mut first: Option<(f64, f64, Point)> = None;
    for u in a.windows(2) {
        let [ux, uy] = std::array::from_fn(|j| u[1].point[j] - u[0].point[j]);
        for v in b.windows(2) {
            let [vx, vy] = std::array::from_fn(|j| v[1].point[j] - v[0].point[j]);
            let det = uy * vx - ux * vy;
            if det.abs() <= 1e-12 {
                continue;
            }
            let [x, y] = std::array::from_fn(|j| v[0].point[j] - u[0].point[j]);
            let s = (y * vx - x * vy) / det;
            let t = (y * ux - x * uy) / det;
            if !(0. ..=1.).contains(&s) || !(0. ..=1.).contains(&t) {
                continue;
            }
            let so = u[0].along + s * (u[1].along - u[0].along);
            let ti = v[0].along + t * (v[1].along - v[0].along);
            if first.is_none_or(|(p, q, _)| so + ti < p + q) {
                let left = lerp(u[0].point, u[1].point, s);
                let right = lerp(v[0].point, v[1].point, t);
                first = Some((so, ti, std::array::from_fn(|j| (left[j] + right[j]) * 0.5)));
            }
        }
    }
    let Some((so, ti, point)) = first else { return };
    if !a.iter().any(|k| k.along > so + 1e-9) || !b.iter().any(|k| k.along > ti + 1e-9) {
        return;
    }
    outgoing.knots.retain(|k| k.along > so + 1e-9);
    outgoing.knots.insert(0, Knot { along: 0., point });
    incoming.knots = b.iter().rev().filter(|k| k.along > ti + 1e-9)
        .map(|k| Knot { along: 1. - k.along, ..*k }).collect();
    incoming.knots.push(Knot { along: 1., point });
}

#[derive(Clone, Copy)]
struct Correction {
    fade: f64,
    delta: Point,
    gradient: [f64; 2],
    on_rim: bool,
}

fn corrections(mesh: &Mesh, p: &Profile) -> Vec<Correction> {
    let samples: Vec<_> = (0..mesh.vertices.len()).map(|i| {
        let (a, d) = coordinates(p.start, p.dir, mesh.world(i));
        let far = sample(&p.far, a, true);
        let rim = [p.start[0] + a * p.dir[0], p.start[1] + a * p.dir[1], sample(&p.local, a, false)[2]];
        (d, std::array::from_fn::<_, 3, _>(|j| far[j] - rim[j]))
    }).collect();
    // One far cell at least. Height differences get a broader shoulder.
    let max_delta = samples.iter().filter(|(d, _)| d.abs() < EDGE_EPS)
        .map(|(_, delta)| delta[2].abs()).fold(0., f64::max);
    let width = p.spacing.dhypot(6. * max_delta);
    samples.into_iter().map(|(d, delta)| {
        let on_rim = d.abs() < EDGE_EPS;
        let t = if on_rim { 0. } else { (d.abs() / width).min(1.) };
        let fade = (1. - t) * (1. - t) * (1. + 2. * t);
        let gradient = -6. * t * (1. - t) * d.signum() * delta[2] / width;
        Correction { fade, delta, gradient: [-p.dir[1] * gradient, p.dir[0] * gradient], on_rim }
    }).collect()
}

fn corner_faces(ends: &[Vertex; 2], vertices: &[Vertex], chain: &[usize]) -> Option<Vec<Face>> {
    let xy = |p: &Vertex| (p[0], p[1]);
    let rim: Vec<_> = [xy(&ends[0])].into_iter().chain(chain.iter().map(|&i| xy(&vertices[i])))
        .chain([xy(&ends[1])]).collect();
    let tris = ear_clip(&rim);
    if tris.len() + 2 != rim.len() {
        return None;
    }
    let mut faces = Vec::new();
    for tri in tris {
        let [a, b, c] = tri.map(|i| rim[i as usize]);
        if (b.0 - a.0) * (c.1 - a.1) - (b.1 - a.1) * (c.0 - a.0) <= 0. {
            return None;
        }
        faces.push(tri.map(|i| i as usize));
    }
    Some(faces)
}

/// Replace a mismatched corner by a small fan containing both independently placed rim endpoints.
fn close_corners(mesh: &mut Mesh, original: &[Vertex], mut changes: Vec<Vec<Correction>>) {
    for (corner, orig) in original.iter().enumerate() {
        let sides: Vec<_> = changes.iter().enumerate().filter_map(|(i, row)| row[corner].on_rim.then_some(i)).collect();
        let [a, b] = sides.as_slice() else { continue };
        let mut ends = [mesh.vertices[corner]; 2];
        for (end, side) in ends.iter_mut().zip([*a, *b]) {
            for j in 0..3 {
                end[j] = orig[j] + changes[side][corner].delta[j];
            }
        }
        let distance2: f64 = (0..3).map(|j| {
            let d = ends[0][j] - ends[1][j];
            d * d
        }).sum();
        if distance2 <= EDGE_EPS * EDGE_EPS {
            continue;
        }
        let mut incident = vec![false; mesh.faces.len()];
        let mut links = BTreeMap::new();
        let mut incoming = BTreeSet::new();
        let mut count = 0;
        for (i, face) in mesh.faces.iter().enumerate() {
            if let Some(at) = face.iter().position(|&v| v == corner) {
                incident[i] = true;
                count += 1;
                links.insert(face[(at + 1) % 3], face[(at + 2) % 3]);
                incoming.insert(face[(at + 2) % 3]);
            }
        }
        let starts: Vec<_> = links.keys().filter(|v| !incoming.contains(v)).copied().collect();
        if starts.len() != 1 || links.len() != count {
            continue;
        }
        let mut chain = starts;
        while chain.len() <= links.len() {
            let Some(&v) = links.get(chain.last().unwrap()) else { break };
            chain.push(v);
        }
        if chain.len() != links.len() + 1 || links.contains_key(chain.last().unwrap()) {
            continue;
        }
        let left = chain[0];
        let right = *chain.last().unwrap();
        let rim_at = |v: usize| [changes[*a][v].on_rim, changes[*b][v].on_rim];
        match (rim_at(left), rim_at(right)) {
            ([true, false], [false, true]) => (),
            ([false, true], [true, false]) => ends.swap(0, 1),
            _ => continue,
        }
        let mut joined = corner_faces(&ends, &mesh.vertices, &chain);
        if joined.is_none() {
            // A close boundary knot can fold the fan. Expand the smallest cavity that triangulates.
            let mut best: Option<(Vec<usize>, Vec<usize>, Vec<Face>)> = None;
            for outgoing in [true, false] {
                let mut expanded = chain.clone();
                let mut added = Vec::new();
                while best.as_ref().is_none_or(|(old, _, _)| added.len() + 1 < old.len()) {
                    let i = if outgoing { 0 } else { expanded.len() - 2 };
                    let (u, v) = (expanded[i], expanded[i + 1]);
                    let mut adjacent = mesh.faces.iter().enumerate().filter(|(j, face)| !incident[*j]
                        && (0..3).any(|k| face[k] == v && face[(k + 1) % 3] == u));
                    let Some((j, face)) = adjacent.next() else { break };
                    if adjacent.next().is_some() || added.contains(&j) {
                        break;
                    }
                    let Some(extra) = face.iter().copied().find(|&w| w != u && w != v) else { break };
                    expanded.insert(i + 1, extra);
                    added.push(j);
                    if let Some(tris) = corner_faces(&ends, &mesh.vertices, &expanded) {
                        best = Some((added, expanded, tris));
                        break;
                    }
                }
            }
            if let Some((added, expanded, tris)) = best {
                for i in added {
                    incident[i] = true;
                }
                chain = expanded;
                joined = Some(tris);
            }
        }
        let Some(faces) = joined else { continue };
        let next = mesh.vertices.len();
        mesh.vertices.extend(ends);
        for row in &mut changes {
            row.push(row[chain[0]]);
            row.push(row[*chain.last().unwrap()]);
        }
        let indices: Vec<_> = [next].into_iter().chain(chain).chain([next + 1]).collect();
        let mut i = 0;
        mesh.faces.retain(|_| { let keep = !incident[i]; i += 1; keep });
        mesh.faces.extend(faces.into_iter().map(|f| f.map(|v| indices[v])));
    }
    mesh.compact();
}

fn stitch(mut mesh: Mesh, mut edges: Vec<Edge>) -> Option<Mesh> {
    for i in 0..edges.len() {
        let (first, later) = edges.split_at_mut(i + 1);
        let a = &mut first[i];
        for b in later {
            if a.start == b.end {
                trim_rims(a, b);
            } else if b.start == a.end {
                trim_rims(b, a);
            }
        }
    }
    let profiles: Vec<_> = edges.into_iter().filter_map(|e| prepare(&mut mesh, e)).collect();
    if profiles.is_empty() {
        return None;
    }
    let changes: Vec<_> = profiles.iter().map(|p| corrections(&mesh, p)).collect();
    let original = mesh.vertices.clone();
    for (i, vert) in mesh.vertices.iter_mut().enumerate() {
        let correction = |s: usize| changes[s][i];
        let mut weights = [0.; 4];
        let mut gradients = [0.; 4];
        let mut total = (0..profiles.len()).map(|s| 1. - correction(s).fade).product::<f64>();
        for s in 0..profiles.len() {
            let outside: f64 = (0..profiles.len()).filter(|&k| k != s).map(|k| 1. - correction(k).fade).product();
            weights[s] = correction(s).fade * outside;
            gradients[s] = outside;
        }
        total += weights.iter().sum::<f64>();
        if total <= 1e-12 {
            // Split the corner later. Put it at the mean of the rims in the meantime.
            for s in 0..profiles.len() {
                weights[s] = if (correction(s).fade - 1.).abs() <= 1e-10 { 1. } else { 0. };
            }
            total = weights.iter().sum();
        }
        for (j, v) in vert[..3].iter_mut().enumerate() {
            *v += (0..profiles.len()).map(|s| weights[s] * correction(s).delta[j]).sum::<f64>() / total;
        }
        for j in 0..2 {
            let gradient = (0..profiles.len()).map(|s| gradients[s] * correction(s).gradient[j]).sum::<f64>() / total;
            vert[j + 3] -= vert[5] * gradient;
        }
        let length = ((vert[3] * vert[3] + vert[4] * vert[4]) + vert[5] * vert[5]).sqrt().max(1e-12);
        for v in &mut vert[3..6] {
            *v /= length;
        }
    }
    if profiles.len() > 1 {
        close_corners(&mut mesh, &original, changes);
    }
    Some(mesh)
}

type PyMesh<'py> = (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>);

#[pyfunction]
pub(crate) fn far_handoff_stitch<'py>(
    py: Python<'py>,
    vertices: PyReadonlyArray2<'py, f64>,
    faces: PyReadonlyArray2<'py, i64>,
    origin: Point,
    segments: PyReadonlyArray2<'py, f64>,
    knots: PyReadonlyArray2<'py, f64>,
    offsets: PyReadonlyArray1<'py, i64>,
) -> PyResult<Option<PyMesh<'py>>> {
    let (vertices, faces, segments, knots) = (vertices.as_array(), faces.as_array(), segments.as_array(), knots.as_array());
    let offsets = offsets.as_slice()?;
    if vertices.ncols() != 10 || faces.ncols() != 3 || segments.ncols() != 5 || knots.ncols() != 4
        || segments.nrows() > 4 || offsets.len() != segments.nrows() + 1
        || offsets.first() != Some(&0) || offsets.last() != Some(&(knots.nrows() as i64))
        || offsets.windows(2).any(|w| w[1] < w[0] + 2)
        || faces.iter().any(|&i| i < 0 || i as usize >= vertices.nrows())
        || vertices.iter().chain(segments.iter()).chain(knots.iter()).chain(origin.iter()).any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("far_handoff_stitch: invalid mesh or edge arrays"));
    }
    let mesh = Mesh {
        vertices: vertices.rows().into_iter().map(|r| std::array::from_fn(|j| r[j])).collect(),
        faces: faces.rows().into_iter().map(|r| std::array::from_fn(|j| r[j] as usize)).collect(),
        origin,
    };
    let mut edges = Vec::new();
    for (i, row) in segments.rows().into_iter().enumerate() {
        let span = offsets[i] as usize..offsets[i + 1] as usize;
        let points: Vec<_> = span.map(|j| Knot { along: knots[[j, 0]], point: std::array::from_fn(|k| knots[[j, k + 1]]) }).collect();
        if row[4] <= 0. || points.windows(2).any(|pair| pair[0].along >= pair[1].along) {
            return Err(PyValueError::new_err("far_handoff_stitch: unordered knots or invalid spacing"));
        }
        edges.push(Edge { start: [row[0], row[1]], end: [row[2], row[3]], spacing: row[4], knots: points });
    }
    Ok(py.detach(|| stitch(mesh, edges)).map(|result| {
        let vertices = Array2::from_shape_vec((result.vertices.len(), 10), result.vertices.into_iter().flatten().collect()).unwrap();
        let faces = Array2::from_shape_vec((result.faces.len(), 3), result.faces.into_iter().flatten().map(|i| i as i64).collect()).unwrap();
        (vertices.into_pyarray(py), faces.into_pyarray(py))
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_short_boundary_segment_refans_without_a_fold() {
        let original = [[0., 0., 0.], [0., -52., 0.], [0.13, -52.13, 0.], [64., -52., 0.],
                        [11.9, 0., 0.], [0.108, 0., 0.]];
        let moved = [[0.1487, -0.1205, 0.4], [0.2126, -52.192, 0.4], [0.3419, -52.322, 0.4],
                     [64.108, -52.181, 0.4], [11.9926, -0.0994, 0.4], [0.1697, -0.0946, 0.4]];
        let vertex = |p: Point| [p[0], p[1], p[2], 0., 0., 1., 0., 0., 0., 0.];
        let vertices = moved.into_iter().map(vertex).collect();
        let mut mesh = Mesh { vertices, faces: vec![[0, 1, 5], [5, 1, 2], [5, 2, 3], [5, 3, 4]], origin: [0.; 3] };
        let original: Vec<_> = original.into_iter().map(vertex).collect();
        let ends = [[0.2357, -0.1464, 0.4], [0.0617, -0.0945, 0.4]];
        let changes = [[0, 1, 6], [0, 4, 5]].into_iter().zip(ends).map(|(indices, end)| {
            (0..6).map(|i| Correction { fade: 0., delta: if i == 0 { end } else { [0.; 3] },
                                       gradient: [0.; 2], on_rim: indices.contains(&i) }).collect()
        }).collect();
        close_corners(&mut mesh, &original, changes);
        assert_eq!(mesh.faces.len(), 5);
        for end in ends {
            assert!(mesh.vertices.iter().any(|v| v[..3] == end));
        }
        for face in mesh.faces {
            let [a, b, c] = face.map(|i| mesh.vertices[i]);
            assert!((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) > 0.);
        }
    }
}
