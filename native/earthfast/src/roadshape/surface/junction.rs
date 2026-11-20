//! A ground node with three or more incident ends and no event gets a single
//! paved apron. The incident strips stop at their first full-width cap, and the
//! perimeter connects the outside corners of those caps in angular order.
use super::{add, cross, inside, mul, norm, snapped, sub, Kept, Line, Mesh, P2};

pub(super) const KIND: i64 = 5;

fn hull(mut pts: Vec<P2>) -> Vec<P2> {
    pts.sort_by(|a, b| a[0].total_cmp(&b[0]).then(a[1].total_cmp(&b[1])));
    pts.dedup();
    fn half(points: impl Iterator<Item = P2>) -> Vec<P2> {
        let mut out = Vec::new();
        for p in points {
            while out.len() >= 2 {
                let n = out.len();
                if cross(sub(out[n - 1], out[n - 2]), sub(p, out[n - 1])) > 0.0 {
                    break;
                }
                out.pop();
            }
            out.push(p);
        }
        out.pop();
        out
    }
    let mut poly = half(pts.iter().copied());
    poly.extend(half(pts.iter().rev().copied()));
    poly
}

pub(super) fn build(
    roads: &[Line],
    kept: &[Kept],
    crossings: &[(usize, f64)],
    mesh: &mut Mesh,
    boxm: &[f64; 4],
) -> Vec<[f64; 2]> {
    let mut spans: Vec<_> = roads.iter().map(|r| [0.0, r.len()]).collect();
    let mut ends = Vec::new();
    for (i, r) in roads.iter().enumerate() {
        if r.p.len() < 2 {
            continue;
        }
        ends.push((i, 0, r.p[0]));
        ends.push((i, 1, r.p[r.p.len() - 1]));
    }
    let mut used = vec![false; ends.len()];
    for a in 0..ends.len() {
        if used[a] {
            continue;
        }
        let node = ends[a].2;
        let group: Vec<_> = (a..ends.len())
            .filter(|&j| !used[j] && norm(sub(ends[j].2, node)) <= 0.01)
            .collect();
        for &j in &group {
            used[j] = true;
        }
        if group.len() < 3 {
            continue;
        }
        let reach = group
            .iter()
            .map(|&j| roads[ends[j].0].hw)
            .fold(0.0, f64::max)
            * 1.5;
        let mut caps = Vec::new();
        let mut cuts = Vec::new();
        let mut blocked = false;
        for &j in &group {
            let (i, e, _) = ends[j];
            let r = &roads[i];
            let offset = reach.min(0.4 * r.len());
            let station = if e == 0 { offset } else { r.len() - offset };
            let (p, n) = r.row(station);
            let left = snapped(add(p, mul(n, r.hw)));
            let right = snapped(sub(p, mul(n, r.hw)));
            if kept.iter().any(|k| {
                [node, p, left, right]
                    .iter()
                    .any(|&q| inside(&k.raw_outline, q))
            }) || crossings
                .iter()
                .any(|&(ri, s)| ri == i && (if e == 0 { s } else { r.len() - s }) <= offset)
            {
                blocked = true;
                break;
            }
            caps.extend([left, right]);
            cuts.push((i, e, station));
        }
        if blocked {
            continue;
        }
        let poly = hull(caps);
        if poly.len() < 3 {
            continue;
        }
        for (i, e, s) in cuts {
            spans[i][e] = s;
        }
        if (0..2).any(|axis| {
            poly.iter().all(|p| p[axis] < boxm[axis])
                || poly.iter().all(|p| p[axis] > boxm[axis + 2])
        }) {
            continue;
        }
        let owner = group.iter().map(|&j| ends[j].0).min().unwrap();
        // The mean is interior even at a sharp turn whose cap hull excludes
        // the mapped node. Each hull edge is a constrained apron boundary.
        let center = mul(
            poly.iter().copied().fold([0.0, 0.0], add),
            1.0 / poly.len() as f64,
        );
        let base = mesh.v.len() as i64;
        for p in std::iter::once(center).chain(poly.iter().copied()) {
            mesh.v.push(p);
            mesh.uv.push(sub(p, node));
        }
        for j in 0..poly.len() {
            mesh.t.push([
                base,
                base + 1 + j as i64,
                base + 1 + ((j + 1) % poly.len()) as i64,
            ]);
            mesh.owner.push(-1 - owner as i64);
            mesh.kind.push(KIND);
        }
    }
    spans
}
