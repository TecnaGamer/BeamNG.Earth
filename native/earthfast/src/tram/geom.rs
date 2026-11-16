//! Planar helpers of the tram overhead line (plan 046): polylines' arc lengths, the point and direction at an arc, the
//! radius round it, distances to polylines, the nearest point of one, a point against building outlines, and the stable
//! hash that phases the poles. They are catenary.py's `_arc`, `_frame_at`, `_radius`, `_dist_to_lines`, `_nearest`,
//! `near_ring` and `_h01` (the Python's numbers: libm's hypot, numpy's clip, searchsorted and fixed-order sums).
use crate::dm::Dm;
use blake2::{digest::consts::U4, Blake2s, Digest};

pub type P2 = [f64; 2];

#[inline]
pub fn hyp(x: f64, y: f64) -> f64 {
    x.dhypot(y)
}

#[inline]
pub fn dist(a: P2, b: P2) -> f64 {
    hyp(a[0] - b[0], a[1] - b[1])
}

/// Python's round(x, 2) (the correctly rounded decimal of the exact binary value, ties to even).
pub fn round_to(x: f64, places: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{:.*}", places, x).parse().unwrap_or(x)
}

/// catenary._h01: a stable fraction of [0, 1) from a key and a salt (blake2s, 4 bytes, little endian).
pub fn h01(key: &str, salt: &str) -> f64 {
    let d = Blake2s::<U4>::digest(format!("{key}/{salt}").as_bytes());
    u32::from_le_bytes([d[0], d[1], d[2], d[3]]) as f64 / 4294967296.0
}

/// catenary._arc: the arc length at each vertex of xy.
pub fn arc(xy: &[P2]) -> Vec<f64> {
    let mut out = Vec::with_capacity(xy.len());
    let mut s = 0.0;
    out.push(0.0);
    for w in xy.windows(2) {
        s += hyp(w[1][0] - w[0][0], w[1][1] - w[0][1]);
        out.push(s);
    }
    out
}

/// catenary._frame_at: (point, unit direction) of polyline xy at arc length s (arc: its arc lengths).
pub fn frame_at(xy: &[P2], arc: &[f64], s: f64) -> (P2, P2) {
    let last = arc[arc.len() - 1];
    let s = if s < 0.0 { 0.0 } else if s > last { last } else { s };
    let ss = arc.partition_point(|&a| a < s) as i64;
    let j = (ss - 1).clamp(0, xy.len() as i64 - 2) as usize;
    let d = [xy[j + 1][0] - xy[j][0], xy[j + 1][1] - xy[j][1]];
    let n = hyp(d[0], d[1]).max(1e-9);
    let t = (s - arc[j]) / n;
    ([xy[j][0] + d[0] * t, xy[j][1] + d[1] * t], [d[0] / n, d[1] / n])
}

/// catenary._radius: the radius of the line round arc s from its heading change over +-win m (inf on a straight).
pub fn radius(xy: &[P2], arc: &[f64], s: f64, win: f64) -> f64 {
    let (_, a) = frame_at(xy, arc, s - win);
    let (_, b) = frame_at(xy, arc, s + win);
    let ang = (a[0] * b[1] - a[1] * b[0]).datan2(a[0] * b[0] + a[1] * b[1]).abs();
    if ang < 1e-4 {
        f64::INFINITY
    } else {
        2.0 * win / ang
    }
}

/// The nearest point of segment a-b to p: (t in [0, 1], the point, squared-length clamp as the Python's `seg`).
#[inline]
pub fn seg_t(p: P2, a: P2, b: P2) -> (f64, P2, f64) {
    let d = [b[0] - a[0], b[1] - a[1]];
    let seg = (d[0] * d[0] + d[1] * d[1]).max(1e-12);
    let t = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / seg).clamp(0.0, 1.0);
    (t, [a[0] + t * d[0], a[1] + t * d[1]], seg)
}

/// catenary._dist_to_lines for one polyline: the distance from p to its nearest point (inf with fewer than two points).
pub fn dist_to_line(p: P2, ln: &[P2]) -> f64 {
    let mut best = f64::INFINITY;
    for w in ln.windows(2) {
        let (_, q, _) = seg_t(p, w[0], w[1]);
        let d = hyp(q[0] - p[0], q[1] - p[1]);
        if d < best {
            best = d;
        }
    }
    best
}

/// catenary._dist_to_lines: the distance from p to the nearest of the polylines.
pub fn dist_to_lines<'a, I: IntoIterator<Item = &'a [P2]>>(p: P2, lines: I) -> f64 {
    let mut best = f64::INFINITY;
    for ln in lines {
        let d = dist_to_line(p, ln);
        if d < best {
            best = d;
        }
    }
    best
}

/// catenary._nearest: (arc s, point, unit direction, distance) of polyline xy at its point nearest p (the first of equals).
pub fn nearest(xy: &[P2], arc: &[f64], p: P2) -> (f64, P2, P2, f64) {
    let mut best = (f64::INFINITY, 0usize, 0.0f64, [0.0; 2], 1.0f64, [0.0; 2]);
    for j in 0..xy.len() - 1 {
        let (t, q, seg) = seg_t(p, xy[j], xy[j + 1]);
        let d = hyp(q[0] - p[0], q[1] - p[1]);
        if d < best.0 {
            best = (d, j, t, q, seg, [xy[j + 1][0] - xy[j][0], xy[j + 1][1] - xy[j][1]]);
        }
    }
    let (d, j, t, q, seg, dv) = best;
    let sq = seg.sqrt();
    (arc[j] + t * sq, q, [dv[0] / sq, dv[1] / sq], d)
}

/// catenary.near_ring: whether p is inside one of the footprints or within m of an outline.
pub fn near_ring(p: P2, rings: &[Vec<P2>], m: f64) -> bool {
    for r in rings {
        if r.len() < 2 {
            continue;
        }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for q in r {
            x0 = x0.min(q[0]);
            x1 = x1.max(q[0]);
            y0 = y0.min(q[1]);
            y1 = y1.max(q[1]);
        }
        if p[0] < x0 - m || p[0] > x1 + m || p[1] < y0 - m || p[1] > y1 + m {
            continue;
        }
        let n = r.len();
        let mut inside = false;
        let mut near = f64::INFINITY;
        for i in 0..n {
            let (a, b) = (r[i], r[(i + 1) % n]);
            let (_, q, _) = seg_t(p, a, b);
            near = near.min(hyp(q[0] - p[0], q[1] - p[1]));
            // (inside: an odd number of the outline's edges cross a ray to +x)
            let (y0e, y1e) = (a[1], b[1]);
            if (y0e > p[1]) != (y1e > p[1]) {
                let den = if y1e == y0e { 1e-12 } else { y1e - y0e };
                if p[0] < (b[0] - a[0]) * (p[1] - y0e) / den + a[0] {
                    inside = !inside;
                }
            }
        }
        if near <= m || inside {
            return true;
        }
    }
    false
}

/// np.linspace(0, 1, k + 1) (its step is 1 / k and its last value exactly 1).
pub fn unit_steps(k: usize) -> Vec<f64> {
    let step = 1.0 / k as f64;
    let mut t: Vec<f64> = (0..=k).map(|i| i as f64 * step).collect();
    t[k] = 1.0;
    t
}

/// The polyline between arcs sa and sb of xy: the point at sa, the vertices between, the point at sb.
pub fn between(xy: &[P2], arc: &[f64], sa: f64, sb: f64) -> Vec<P2> {
    let mut out = vec![frame_at(xy, arc, sa).0];
    for k in 0..xy.len() {
        if arc[k] > sa && arc[k] < sb {
            out.push(xy[k]);
        }
    }
    out.push(frame_at(xy, arc, sb).0);
    out
}

/// Douglas-Peucker: the indices of pts kept so that no dropped vertex is farther than tol from the kept polyline
/// (the first and last always kept).
pub fn simplify(pts: &[P2], tol: f64) -> Vec<usize> {
    let n = pts.len();
    if n <= 2 {
        return (0..n).collect();
    }
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[n - 1] = true;
    let mut stack = vec![(0usize, n - 1)];
    while let Some((a, b)) = stack.pop() {
        let (mut worst, mut wi) = (0.0, a);
        for i in a + 1..b {
            let (_, q, _) = seg_t(pts[i], pts[a], pts[b]);
            let d = hyp(q[0] - pts[i][0], q[1] - pts[i][1]);
            if d > worst {
                worst = d;
                wi = i;
            }
        }
        if worst > tol {
            keep[wi] = true;
            stack.push((a, wi));
            stack.push((wi, b));
        }
    }
    (0..n).filter(|&i| keep[i]).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arc_and_frame() {
        let xy = [[0.0, 0.0], [3.0, 4.0], [3.0, 10.0]];
        let a = arc(&xy);
        assert_eq!(a, vec![0.0, 5.0, 11.0]);
        let (p, u) = frame_at(&xy, &a, 8.0);
        assert!((p[0] - 3.0).abs() < 1e-12 && (p[1] - 7.0).abs() < 1e-12);
        assert!((u[0]).abs() < 1e-12 && (u[1] - 1.0).abs() < 1e-12);
        let (p, _) = frame_at(&xy, &a, -4.0);
        assert_eq!(p, [0.0, 0.0]);
    }

    #[test]
    fn radius_of_a_straight_and_a_circle() {
        let xy: Vec<P2> = (0..=100).map(|i| [i as f64, 0.0]).collect();
        let a = arc(&xy);
        assert!(radius(&xy, &a, 50.0, 20.0).is_infinite());
        let r = 100.0;
        let xy: Vec<P2> = (0..=200).map(|i| { let t = i as f64 * 0.005; [r * t.dsin(), r * (1.0 - t.dcos())] }).collect();
        let a = arc(&xy);
        let got = radius(&xy, &a, 50.0, 20.0);
        assert!((got - r).abs() / r < 0.01, "{got}");
    }

    #[test]
    fn nearest_and_distance() {
        let ln = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]];
        let a = arc(&ln);
        let (s, q, u, d) = nearest(&ln, &a, [5.0, 3.0]);
        assert!((s - 5.0).abs() < 1e-12 && q == [5.0, 0.0] && u == [1.0, 0.0] && (d - 3.0).abs() < 1e-12);
        assert!((dist_to_line([12.0, 5.0], &ln) - 2.0).abs() < 1e-12);
        assert!(dist_to_line([1.0, 1.0], &ln[..1]).is_infinite());
    }

    #[test]
    fn rings() {
        let r = vec![vec![[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]];
        assert!(near_ring([5.0, 5.0], &r, 0.5), "inside");
        assert!(near_ring([10.4, 5.0], &r, 0.5), "near the outline");
        assert!(!near_ring([11.0, 5.0], &r, 0.5));
    }

    #[test]
    fn hash_is_stable_and_in_range() {
        let h = h01("abc", "tram");
        assert!((0.0..1.0).contains(&h));
        assert_eq!(h, h01("abc", "tram"));
        assert_ne!(h, h01("abd", "tram"));
    }

    #[test]
    fn between_and_simplify() {
        let xy: Vec<P2> = (0..=20).map(|i| [i as f64, 0.0]).collect();
        let a = arc(&xy);
        let b = between(&xy, &a, 2.5, 7.5);
        assert_eq!(b.len(), 7);
        assert_eq!(b[0], [2.5, 0.0]);
        assert_eq!(b[6], [7.5, 0.0]);
        assert_eq!(simplify(&b, 0.01), vec![0, 6], "a straight line keeps its ends");
        let arc_pts: Vec<P2> = (0..=20).map(|i| { let t = i as f64 * 0.05; [30.0 * t.dsin(), 30.0 * (1.0 - t.dcos())] }).collect();
        let k = simplify(&arc_pts, 0.10);
        assert!(k.len() > 2 && k.len() < arc_pts.len());
        for i in 0..arc_pts.len() {
            let mut best = f64::INFINITY;
            for w in k.windows(2) {
                let (_, q, _) = seg_t(arc_pts[i], arc_pts[w[0]], arc_pts[w[1]]);
                best = best.min(hyp(q[0] - arc_pts[i][0], q[1] - arc_pts[i][1]));
            }
            assert!(best <= 0.10 + 1e-9);
        }
    }

    #[test]
    fn steps_and_rounding() {
        assert_eq!(unit_steps(4), vec![0.0, 0.25, 0.5, 0.75, 1.0]);
        assert_eq!(round_to(2.675, 2), 2.67);
        assert_eq!(round_to(0.125, 2), 0.12);
    }
}
