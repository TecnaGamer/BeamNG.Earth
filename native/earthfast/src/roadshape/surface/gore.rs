//! A painted gore is bounded by two ordered lane edges. Seed their polygon with
//! row triangles, then legalise all unconstrained edges with integer-mm predicates.
//! Every fourth cross-section is a drape constraint (8 m at S1's sampling).
use super::{Mesh, P2};
use std::collections::BTreeMap;

type I2 = [i128; 2];
fn mm(p: P2) -> I2 {
    p.map(|v| (1000.0 * v).round() as i128)
}
fn rel(a: I2, b: I2) -> I2 {
    [a[0] - b[0], a[1] - b[1]]
}
fn det(a: I2, b: I2) -> i128 {
    a[0] * b[1] - a[1] * b[0]
}
fn orient(a: I2, b: I2, c: I2) -> i128 {
    det(rel(b, a), rel(c, a))
}
fn length2(a: I2) -> i128 {
    a[0] * a[0] + a[1] * a[1]
}
fn incircle(a: I2, b: I2, c: I2, d: I2) -> i128 {
    let (a, b, c) = (rel(a, d), rel(b, d), rel(c, d));
    length2(a) * det(b, c) + length2(b) * det(c, a) + length2(c) * det(a, b)
}

fn legalise(points: &[I2], tris: &mut [[usize; 3]]) {
    loop {
        let mut edges = BTreeMap::new();
        let mut flip = None;
        'search: for (i, tri) in tris.iter().enumerate() {
            for k in 0..3 {
                let (a, b, c) = (tri[k], tri[(k + 1) % 3], tri[(k + 2) % 3]);
                if let Some(&(d, j)) = edges.get(&(b, a)) {
                    // A strictly inside circumcircle: ties keep the canonical seed.
                    let (pa, pb, pc, pd) = (points[a], points[b], points[c], points[d]);
                    if orient(pc, pd, pb) > 0
                        && orient(pd, pc, pa) > 0
                        && incircle(pa, pb, pc, pd) > 0
                    {
                        flip = Some((i, [c, d, b], j, [d, c, a]));
                        break 'search;
                    }
                }
                edges.insert((a, b), (c, i));
            }
        }
        match flip {
            Some((i, a, j, b)) => {
                tris[i] = a;
                tris[j] = b;
            }
            None => break,
        }
    }
}

pub(super) fn zone(
    mesh: &mut Mesh,
    base: usize,
    stride: usize,
    rows: usize,
    k: usize,
    owner: i64,
    boxm: &[f64; 4],
) {
    const ROWS_PER_PATCH: usize = 4;
    for start in (0..rows - 1).step_by(ROWS_PER_PATCH) {
        let end = (start + ROWS_PER_PATCH).min(rows - 1);
        let mut ids = Vec::new();
        let mut points = Vec::new();
        let mut tris = Vec::new();
        let mut prev: Option<[usize; 2]> = None;
        for r in start..=end {
            let idx = base + r * stride + k;
            let pair = [idx, idx + 1].map(|id| {
                let p = mm(mesh.v[id]);
                if let Some(j) = points.iter().position(|q| *q == p) {
                    return j;
                }
                points.push(p);
                ids.push(id);
                points.len() - 1
            });
            if let Some([a, b]) = prev {
                let [c, d] = pair;
                // Concave row quads have just one interior diagonal.
                let candidates = if orient(points[a], points[b], points[d]) < 0
                    || orient(points[a], points[d], points[c]) < 0
                {
                    [[a, b, c], [b, d, c]]
                } else {
                    [[a, b, d], [a, d, c]]
                };
                tris.extend(
                    candidates
                        .into_iter()
                        .filter(|t| orient(points[t[0]], points[t[1]], points[t[2]]) > 0),
                );
            }
            prev = Some(pair);
        }
        legalise(&points, &mut tris);
        for t in tris {
            let t = t.map(|i| ids[i]);
            let pts = t.map(|i| mesh.v[i]);
            if (0..2).any(|a| {
                pts.iter().all(|p| p[a] < boxm[a]) || pts.iter().all(|p| p[a] > boxm[a + 2])
            }) {
                continue;
            }
            mesh.t.push(t.map(|i| i as i64));
            mesh.owner.push(owner);
            mesh.kind.push(super::K_GO);
        }
    }
}
