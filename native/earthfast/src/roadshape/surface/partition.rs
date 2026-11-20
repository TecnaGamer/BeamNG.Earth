//! Partition the common pavement of independently sampled carriageways in plan view.
//! Over a road face its transverse texture coordinate is affine. The road with the
//! smaller |2u - 1| owns the point. This gives parallel arms a width-weighted median
//! and meeting arms an angular bisector, even at a junction with no S1 event.
//! Event lanes have precedence over ordinary roads. Stable face order breaks ties.
use super::{cross, dot, left, lerp, sub, Mesh, P2};
use std::collections::BTreeMap;

#[derive(Clone, Copy)]
struct Plane {
    gradient: P2,
    origin: P2,
    value: f64,
}

impl Plane {
    fn at(&self, p: P2) -> f64 {
        self.value + dot(self.gradient, sub(p, self.origin))
    }
}

struct Face {
    points: [P2; 3],
    planes: [Plane; 3],
    u: Plane,
    v: Plane,
    bounds: [f64; 4],
    owner: i64,
    apron: bool,
}

impl Face {
    fn new(mesh: &Mesh, i: usize) -> Self {
        let ids = mesh.t[i].map(|x| x as usize);
        let points = ids.map(|j| mesh.v[j]);
        let [a, b, c] = points;
        let ab = sub(b, a);
        let ac = sub(c, a);
        let det = cross(ab, ac);
        let attribute = |k| {
            let values: [f64; 3] = ids.map(|j| mesh.uv[j][k]);
            let db = values[1] - values[0];
            let dc = values[2] - values[0];
            Plane {
                origin: a,
                value: values[0],
                gradient: [
                    (db * ac[1] - dc * ab[1]) / det,
                    (ab[0] * dc - ac[0] * db) / det,
                ],
            }
        };
        Self {
            points,
            planes: std::array::from_fn(|k| Plane {
                origin: points[k],
                value: 0.0,
                gradient: left(sub(points[(k + 1) % 3], points[k])),
            }),
            u: attribute(0),
            v: attribute(1),
            owner: mesh.owner[i],
            apron: mesh.kind[i] == super::junction::KIND,
            bounds: [
                a[0].min(b[0]).min(c[0]),
                a[1].min(b[1]).min(c[1]),
                a[0].max(b[0]).max(c[0]),
                a[1].max(b[1]).max(c[1]),
            ],
        }
    }
    fn grid(&self) -> [i64; 4] {
        self.bounds.map(|v| (v / 16.0).floor() as i64)
    }
    fn meets(&self, other: &Face) -> bool {
        self.bounds[0] < other.bounds[2]
            && self.bounds[1] < other.bounds[3]
            && self.bounds[2] > other.bounds[0]
            && self.bounds[3] > other.bounds[1]
            && self
                .planes
                .iter()
                .all(|plane| other.points.iter().any(|&p| plane.at(p) > 1e-9))
            && other
                .planes
                .iter()
                .all(|plane| self.points.iter().any(|&p| plane.at(p) > 1e-9))
    }
}

// Partition a convex polygon along one line. Both children use the same crossing.
fn split(poly: &[P2], plane: &Plane) -> (Vec<P2>, Vec<P2>) {
    let mut inside = Vec::new();
    let mut outside = Vec::new();
    let mut a = poly[poly.len() - 1];
    let mut da = plane.at(a);
    for &b in poly {
        let db = plane.at(b);
        if da * db < 0.0 {
            let c = lerp(a, b, da / (da - db));
            inside.push(c);
            outside.push(c);
        }
        if db >= 0.0 {
            inside.push(b);
        }
        if db <= 0.0 {
            outside.push(b);
        }
        a = b;
        da = db;
    }
    (inside, outside)
}

fn subtract(parts: &mut Vec<Vec<P2>>, planes: &[Plane]) {
    let mut next = Vec::new();
    for original in parts.drain(..) {
        let mut inside = original.clone();
        let start = next.len();
        for plane in planes {
            if inside.iter().all(|&p| plane.at(p) <= 0.0) {
                inside.clear();
                break;
            }
            if inside.iter().all(|&p| plane.at(p) >= 0.0) {
                continue;
            }
            let (a, b) = split(&inside, plane);
            if b.len() >= 3 {
                next.push(b);
            }
            inside = a;
            if inside.len() < 3 {
                break;
            }
        }
        if inside.len() < 3 {
            next.truncate(start);
            next.push(original);
        }
    }
    *parts = next;
}

// Inside the other's footprint it wins where |u_other - .5| < |u_self - .5|.
// The two signs of u_self - .5 give two convex regions. Coincident lanes can
// tie across a whole face: use the canonical input order for that region.
fn divide(parts: &mut Vec<Vec<P2>>, a: &Face, b: &Face, earlier: bool) {
    if a.owner == b.owner || a.owner >= 0 || b.owner >= 0 || a.apron || b.apron {
        if earlier {
            subtract(parts, &b.planes);
        }
        return;
    }
    for s in [-1.0, 1.0] {
        let mut planes = b.planes.to_vec();
        let mut loses = true;
        for t in [-1.0, 1.0] {
            let plane = Plane {
                origin: a.u.origin,
                value: s * (a.u.value - 0.5) + t * (b.u.at(a.u.origin) - 0.5),
                gradient: [
                    s * a.u.gradient[0] + t * b.u.gradient[0],
                    s * a.u.gradient[1] + t * b.u.gradient[1],
                ],
            };
            if a.points.iter().all(|&p| plane.at(p).abs() < 1e-12) {
                if !earlier {
                    loses = false;
                    break;
                }
            } else {
                planes.push(plane);
            }
        }
        if loses {
            subtract(parts, &planes);
        }
    }
}

pub(super) fn apply(mesh: &mut Mesh) {
    let faces: Vec<_> = (0..mesh.t.len()).map(|i| Face::new(mesh, i)).collect();
    let mut grid: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
    for (i, f) in faces.iter().enumerate() {
        let [x0, y0, x1, y1] = f.grid();
        for y in y0..=y1 {
            for x in x0..=x1 {
                grid.entry((x, y)).or_default().push(i);
            }
        }
    }
    let (tris, owners, kinds) = (
        std::mem::take(&mut mesh.t),
        std::mem::take(&mut mesh.owner),
        std::mem::take(&mut mesh.kind),
    );
    let mut neighbours = Vec::new();
    for (i, a) in faces.iter().enumerate() {
        let [x0, y0, x1, y1] = a.grid();
        neighbours.clear();
        for y in y0..=y1 {
            for x in x0..=x1 {
                neighbours.extend_from_slice(&grid[&(x, y)]);
            }
        }
        neighbours.sort_unstable();
        neighbours.dedup();
        let mut parts = Vec::new();
        let mut changed = false;
        for &j in &neighbours {
            if i == j
                || (j > i && (a.owner >= 0 || a.apron || a.owner == faces[j].owner))
                || !a.meets(&faces[j])
            {
                continue;
            }
            if !changed {
                parts.push(a.points.to_vec());
                changed = true;
            }
            divide(&mut parts, a, &faces[j], j < i);
            if parts.is_empty() {
                break;
            }
        }
        if !changed || (parts.len() == 1 && parts[0] == a.points) {
            mesh.t.push(tris[i]);
            mesh.owner.push(owners[i]);
            mesh.kind.push(kinds[i]);
            continue;
        }
        for poly in parts {
            let base = mesh.v.len() as i64;
            for &p in &poly {
                mesh.v.push(p);
                mesh.uv.push([a.u.at(p), a.v.at(p)]);
            }
            for k in 2..poly.len() {
                if cross(sub(poly[k - 1], poly[0]), sub(poly[k], poly[0])) > 1e-10 {
                    mesh.t.push([base, base + k as i64 - 1, base + k as i64]);
                    mesh.owner.push(owners[i]);
                    mesh.kind.push(kinds[i]);
                }
            }
        }
    }
}
