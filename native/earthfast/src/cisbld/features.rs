//! A footprint's shape: its smallest oriented box (long side L, short side W, long axis u), how much of the box it fills,
//! its corners. Pure, deterministic (libm through `Dm`).
use crate::dm::Dm;

pub struct Feat {
    pub cx: f64,
    pub cy: f64,
    /// Unit vector along the long side.
    pub ux: f64,
    pub uy: f64,
    pub l: f64,
    pub w: f64,
    pub area: f64,
    /// area / (L * W): 1 for a rectangle.
    pub rect: f64,
    /// Vertices where the simplified outline turns by more than CORNER_DEG (stair cores and loggias are simplified away).
    pub corners: u32,
    /// The width of the body: of a bar its short side, of an L or a U its arms' width (from area and perimeter of the
    /// simplified outline: d = (P - sqrt(P^2 - 16 A)) / 4, exact for a rectangle).
    pub arm: f64,
}

const CORNER_DEG: f64 = 25.0;
/// m: the outline is simplified to this before its corners and perimeter are counted.
const SIMPLIFY_M: f64 = 1.5;
const MAX_OBB_VERTS: usize = 300;

/// The ring without a closing duplicate and without repeated points.
fn clean(xs: &[f64], ys: &[f64]) -> Vec<(f64, f64)> {
    let mut v: Vec<(f64, f64)> = Vec::with_capacity(xs.len());
    for i in 0..xs.len().min(ys.len()) {
        if let Some(&(px, py)) = v.last() {
            if (px - xs[i]).abs() < 1e-6 && (py - ys[i]).abs() < 1e-6 {
                continue;
            }
        }
        v.push((xs[i], ys[i]));
    }
    if v.len() > 1 {
        let (a, b) = (v[0], v[v.len() - 1]);
        if (a.0 - b.0).abs() < 1e-6 && (a.1 - b.1).abs() < 1e-6 {
            v.pop();
        }
    }
    v
}

/// Douglas-Peucker on a closed ring: the vertices that stay.
fn simplify(p: &[(f64, f64)], tol: f64) -> Vec<(f64, f64)> {
    let n = p.len();
    if n <= 4 {
        return p.to_vec();
    }
    // split the ring at vertex 0 and its farthest vertex, simplify both halves
    let far = (1..n).max_by(|&a, &b| {
        let da = (p[a].0 - p[0].0).dhypot(p[a].1 - p[0].1);
        let db = (p[b].0 - p[0].0).dhypot(p[b].1 - p[0].1);
        da.partial_cmp(&db).unwrap_or(std::cmp::Ordering::Equal)
    });
    let Some(far) = far else { return p.to_vec() };
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[far] = true;
    let mut stack: Vec<(usize, usize)> = vec![(0, far), (far, n)];
    while let Some((a, b)) = stack.pop() {
        let (ax, ay) = p[a];
        let (bx, by) = p[b % n];
        let (dx, dy) = (bx - ax, by - ay);
        let len = dx.dhypot(dy);
        let mut best = (0.0, usize::MAX);
        for k in a + 1..b {
            let d = if len < 1e-9 {
                (p[k].0 - ax).dhypot(p[k].1 - ay)
            } else {
                ((p[k].0 - ax) * dy - (p[k].1 - ay) * dx).abs() / len
            };
            if d > best.0 {
                best = (d, k);
            }
        }
        if best.0 > tol && best.1 != usize::MAX {
            keep[best.1] = true;
            stack.push((a, best.1));
            stack.push((best.1, b));
        }
    }
    (0..n).filter(|&i| keep[i]).map(|i| p[i]).collect()
}

fn perimeter(p: &[(f64, f64)]) -> f64 {
    let n = p.len();
    (0..n).map(|i| (p[(i + 1) % n].0 - p[i].0).dhypot(p[(i + 1) % n].1 - p[i].1)).sum()
}

fn shoelace(p: &[(f64, f64)]) -> f64 {
    let n = p.len();
    let (x0, y0) = p[0];
    let mut a2 = 0.0;
    for i in 0..n {
        let j = (i + 1) % n;
        a2 += (p[i].0 - x0) * (p[j].1 - y0) - (p[j].0 - x0) * (p[i].1 - y0);
    }
    (a2 * 0.5).abs()
}

/// The features of a ring, None under three distinct vertices or without area.
pub fn features(xs: &[f64], ys: &[f64]) -> Option<Feat> {
    let p = clean(xs, ys);
    let n = p.len();
    if n < 3 {
        return None;
    }
    let area = shoelace(&p);
    if area < 1.0 {
        return None;
    }
    // the box: the least-area one among those flush with an edge (the rotating-callipers answer for a polygon)
    let step = n.div_ceil(MAX_OBB_VERTS).max(1);
    let mut best: Option<(f64, [f64; 2], f64, f64, f64, f64)> = None; // area, u, umin, umax, vmin, vmax
    for i in (0..n).step_by(step) {
        let j = (i + 1) % n;
        let (dx, dy) = (p[j].0 - p[i].0, p[j].1 - p[i].1);
        let len = dx.dhypot(dy);
        if len < 0.5 {
            continue;
        }
        let (ux, uy) = (dx / len, dy / len);
        let (mut u0, mut u1, mut v0, mut v1) = (f64::MAX, f64::MIN, f64::MAX, f64::MIN);
        for q in p.iter() {
            let a = q.0 * ux + q.1 * uy;
            let b = -q.0 * uy + q.1 * ux;
            u0 = u0.min(a);
            u1 = u1.max(a);
            v0 = v0.min(b);
            v1 = v1.max(b);
        }
        let ar = (u1 - u0) * (v1 - v0);
        if best.as_ref().is_none_or(|b| ar < b.0 - 1e-9) {
            best = Some((ar, [ux, uy], u0, u1, v0, v1));
        }
    }
    let (_, u, u0, u1, v0, v1) = best?;
    let (mut l, mut w) = (u1 - u0, v1 - v0);
    let (mut ux, mut uy) = (u[0], u[1]);
    let (cu, cv) = ((u0 + u1) * 0.5, (v0 + v1) * 0.5);
    let (mut cx, mut cy) = (cu * ux - cv * uy, cu * uy + cv * ux);
    if w > l {
        std::mem::swap(&mut l, &mut w);
        let t = ux;
        ux = -uy;
        uy = t;
    }
    if !(cx.is_finite() && cy.is_finite()) {
        cx = p[0].0;
        cy = p[0].1;
    }
    let q = simplify(&p, SIMPLIFY_M);
    let (qa, qp) = if q.len() >= 3 { (shoelace(&q), perimeter(&q)) } else { (area, perimeter(&p)) };
    let disc = (qp * qp - 16.0 * qa).max(0.0);
    let arm = ((qp - disc.sqrt()) / 4.0).clamp(0.0, w.max(0.0).max(l));
    let mut corners = 0u32;
    let n = q.len();
    let p = if n >= 3 { q } else { p };
    let n = p.len();
    for i in 0..n {
        let (a, b, c) = (p[(i + n - 1) % n], p[i], p[(i + 1) % n]);
        let (v1x, v1y, v2x, v2y) = (b.0 - a.0, b.1 - a.1, c.0 - b.0, c.1 - b.1);
        if v1x.dhypot(v1y) < 0.3 || v2x.dhypot(v2y) < 0.3 {
            continue;
        }
        let turn = (v1x * v2y - v1y * v2x).datan2(v1x * v2x + v1y * v2y).abs().to_degrees();
        if turn > CORNER_DEG {
            corners += 1;
        }
    }
    Some(Feat { cx, cy, ux, uy, l, w, area, rect: (area / (l * w).max(1e-9)).min(1.0), corners, arm })
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn rect_ring(cx: f64, cy: f64, l: f64, w: f64, ang_deg: f64) -> (Vec<f64>, Vec<f64>) {
        let (s, c) = ang_deg.to_radians().dsin_cos();
        let pts = [(-l / 2.0, -w / 2.0), (l / 2.0, -w / 2.0), (l / 2.0, w / 2.0), (-l / 2.0, w / 2.0)];
        (pts.iter().map(|p| cx + p.0 * c - p.1 * s).collect(), pts.iter().map(|p| cy + p.0 * s + p.1 * c).collect())
    }

    #[test]
    fn a_rotated_rectangle_gives_its_sides_and_axis() {
        let (xs, ys) = rect_ring(100.0, 50.0, 64.0, 11.5, 30.0);
        let f = features(&xs, &ys).unwrap();
        assert!((f.l - 64.0).abs() < 1e-6 && (f.w - 11.5).abs() < 1e-6, "{} {}", f.l, f.w);
        assert!((f.ux * 30f64.to_radians().dcos() + f.uy * 30f64.to_radians().dsin()).abs() > 0.9999);
        assert!(f.rect > 0.999 && f.corners == 4);
        assert!((f.cx - 100.0).abs() < 1e-6 && (f.cy - 50.0).abs() < 1e-6);
    }

    #[test]
    fn an_l_shape_fills_less_of_its_box() {
        let xs = [0.0, 40.0, 40.0, 15.0, 15.0, 0.0];
        let ys = [0.0, 0.0, 14.0, 14.0, 30.0, 30.0];
        let f = features(&xs, &ys).unwrap();
        assert!(f.rect < 0.8 && f.corners == 6, "{} {}", f.rect, f.corners);
    }

    #[test]
    fn the_arm_of_a_bar_is_its_width_and_of_an_l_its_arms() {
        let (xs, ys) = rect_ring(0.0, 0.0, 64.0, 11.5, 0.0);
        assert!((features(&xs, &ys).unwrap().arm - 11.5).abs() < 0.05);
        // an L of two 13 m wide slabs, 60 m each
        let xs = [0.0, 60.0, 60.0, 13.0, 13.0, 0.0];
        let ys = [0.0, 0.0, 13.0, 13.0, 60.0, 60.0];
        let a = features(&xs, &ys).unwrap().arm;
        assert!((a - 13.0).abs() < 1.0, "{a}");
        // a square tower: its side
        let (xs, ys) = rect_ring(0.0, 0.0, 24.0, 24.0, 10.0);
        assert!((features(&xs, &ys).unwrap().arm - 24.0).abs() < 0.2);
    }

    #[test]
    fn stair_cores_do_not_count_as_corners() {
        // a 60 x 12 slab with four 1 m deep stair cores on one long side
        let mut xs = vec![0.0];
        let mut ys = vec![0.0];
        for k in 0..4 {
            let x = 8.0 + k as f64 * 14.0;
            xs.extend([x, x, x + 4.0, x + 4.0]);
            ys.extend([0.0, -1.0, -1.0, 0.0]);
        }
        xs.extend([60.0, 60.0, 0.0]);
        ys.extend([0.0, 12.0, 12.0]);
        let f = features(&xs, &ys).unwrap();
        assert!(f.corners <= 6, "{}", f.corners);
        assert!((f.arm - 12.0).abs() < 1.0, "{}", f.arm);
    }

    #[test]
    fn a_degenerate_ring_has_no_features() {
        assert!(features(&[0.0, 1.0], &[0.0, 1.0]).is_none());
        assert!(features(&[0.0, 1.0, 2.0], &[0.0, 0.0, 0.0]).is_none());
    }
}
