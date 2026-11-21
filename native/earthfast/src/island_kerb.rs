//! Trace a street island between the actual carriageway edges. Narrow slip islands can
//! have their map centroid centimetres from a road and an acute corner narrower than
//! the kerb band. Offset a clear inner outline outwards with round joins in that case.
use crate::{dm::Dm, inside, near_any, seg_dist};

type Point = [f64; 2];

pub(super) struct Boundary<'a> {
    pub x: &'a [f64],
    pub y: &'a [f64],
    pub segments: &'a [[f64; 4]],
    pub half: &'a [f64],
}

impl Boundary<'_> {
    pub fn rays(
        &self,
        centre: Point,
        rays: usize,
        step: f64,
        limit: f64,
        refine: bool,
    ) -> Vec<Point> {
        (0..rays)
            .map(|k| {
                let a = 2.0 * std::f64::consts::PI * k as f64 / rays as f64;
                let (ux, uy) = (a.dcos(), a.dsin());
                let point = |r| [centre[0] + ux * r, centre[1] + uy * r];
                let clear = |r| {
                    let [x, y] = point(r);
                    inside(self.x, self.y, x, y) && !near_any(self.segments, self.half, x, y)
                };
                let mut r = 0.0;
                while r < limit {
                    if !clear(r + step) {
                        if refine {
                            let mut hi = r + step;
                            for _ in 0..8 {
                                let mid = (r + hi) * 0.5;
                                if clear(mid) {
                                    r = mid;
                                } else {
                                    hi = mid;
                                }
                            }
                        }
                        break;
                    }
                    r += step;
                }
                point(r)
            })
            .collect()
    }

    fn clearance(&self, [x, y]: Point) -> f64 {
        if !inside(self.x, self.y, x, y) {
            return f64::NEG_INFINITY;
        }
        self.segments
            .iter()
            .zip(self.half)
            .map(|(s, h)| seg_dist(s[0], s[1], s[2], s[3], x, y) - h)
            .fold(f64::INFINITY, f64::min)
    }

    pub fn band(
        &self,
        mut centre: Point,
        rays: usize,
        step: f64,
        limit: f64,
        width: f64,
    ) -> Vec<Point> {
        // Maximise the shortest distance to a bounding carriageway, with a fixed
        // traversal order for ties. Start in the clear component found by islands.find.
        let seed = centre;
        let mut room = self.clearance(centre);
        for delta in [2., 1., 0.5, 0.25, 0.125, 0.0625] {
            for _ in 0..75 {
                let before = centre;
                for (dx, dy) in [
                    (1., 0.),
                    (-1., 0.),
                    (0., 1.),
                    (0., -1.),
                    (1., 1.),
                    (-1., 1.),
                    (1., -1.),
                    (-1., -1.),
                ] {
                    let p = [before[0] + dx * delta, before[1] + dy * delta];
                    let d = self.clearance(p);
                    if d > room {
                        room = d;
                        centre = p;
                    }
                }
                if centre == before {
                    break;
                }
            }
        }
        let reach = limit + (seed[0] - centre[0]).dhypot(seed[1] - centre[1]);
        if room > width {
            let half: Vec<_> = self.half.iter().map(|h| h + width).collect();
            let inner = smooth(
                &Boundary {
                    half: &half,
                    ..*self
                }
                .rays(centre, rays, step, reach, true),
            );
            let area2: f64 = inner
                .iter()
                .zip(inner.iter().cycle().skip(1))
                .map(|(a, b)| {
                    (a[0] - centre[0]) * (b[1] - centre[1])
                        - (a[1] - centre[1]) * (b[0] - centre[0])
                })
                .sum();
            // A wide enough kerb can surround a very small core. The minimum
            // island area applies to the finished outline, not to this inset.
            if area2 > 1e-6 {
                return outer_boundary(round_outside(&inner, width), centre);
            }
        }
        // A sliver may hold a kerb but have no usable inner loop.
        smooth(&self.rays(centre, rays, step, reach, true))
    }
}

fn smooth(p: &[Point]) -> Vec<Point> {
    (0..p.len())
        .map(|i| {
            let (a, b, c) = (p[(i + p.len() - 1) % p.len()], p[i], p[(i + 1) % p.len()]);
            [
                (a[0] + 2. * b[0] + c[0]) / 4.,
                (a[1] + 2. * b[1] + c[1]) / 4.,
            ]
        })
        .collect()
}

fn round_outside(p: &[Point], radius: f64) -> Vec<Point> {
    let normals: Vec<_> = (0..p.len())
        .map(|i| {
            let prev = p[(i + p.len() - 1) % p.len()];
            let (x, y) = (p[i][0] - prev[0], p[i][1] - prev[1]);
            let length = x.dhypot(y).max(1e-10);
            [y / length, -x / length]
        })
        .collect();
    let mut out = Vec::new();
    for (i, &[x, y]) in p.iter().enumerate() {
        let (a, b) = (normals[i], normals[(i + 1) % p.len()]);
        let dot = a[0] * b[0] + a[1] * b[1];
        let turn = (a[0] * b[1] - a[1] * b[0]).datan2(dot);
        if turn <= 0. {
            let d = radius / (1. + dot).max(0.5);
            out.push([x + d * (a[0] + b[0]), y + d * (a[1] + b[1])]);
        } else {
            // Chords at most 15 degrees apart. At a 1.45 m radius their sagitta is 1.3 cm.
            let steps = (turn / (std::f64::consts::PI / 12.)).ceil().max(1.) as usize;
            for j in 0..=steps {
                let angle = turn * j as f64 / steps as f64;
                let (c, s) = (angle.dcos(), angle.dsin());
                out.push([
                    x + radius * (a[0] * c - a[1] * s),
                    y + radius * (a[0] * s + a[1] * c),
                ]);
            }
        }
    }
    out
}

fn cross(a: Point, b: Point) -> f64 {
    a[0] * b[1] - a[1] * b[0]
}

fn sub(a: Point, b: Point) -> Point {
    [a[0] - b[0], a[1] - b[1]]
}

/// The dilation of a star-shaped inset by a disc is still star-shaped about
/// its centre. At a re-entrant join the raw offset can loop inside that
/// dilation. Keep the furthest boundary along each sampled direction.
fn outer_boundary(points: Vec<Point>, centre: Point) -> Vec<Point> {
    let relative: Vec<_> = points.iter().map(|&p| sub(p, centre)).collect();
    let edges = || {
        relative
            .iter()
            .copied()
            .zip(relative.iter().copied().cycle().skip(1))
    };
    if edges().all(|(a, b)| cross(a, b) >= -1e-10) {
        return points;
    }
    let mut rays = Vec::with_capacity(relative.len());
    for &a in &relative {
        let mut reach = a[0].dhypot(a[1]);
        if reach < 1e-8 {
            continue;
        }
        let ray = [a[0] / reach, a[1] / reach];
        for (p, q) in edges() {
            let segment = sub(q, p);
            let det = cross(ray, segment);
            if det.abs() < 1e-10 {
                continue;
            }
            let u = cross(p, ray) / det;
            if (-1e-8..=1.00000001).contains(&u) {
                reach = reach.max(cross(p, segment) / det);
            }
        }
        rays.push((
            ray[1].datan2(ray[0]),
            [centre[0] + reach * ray[0], centre[1] + reach * ray[1]],
        ));
    }
    rays.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut out: Vec<Point> = Vec::new();
    for (_, point) in rays {
        if out
            .last()
            .is_none_or(|last| (point[0] - last[0]).dhypot(point[1] - last[1]) > 1e-4)
        {
            out.push(point);
        }
    }
    out
}

/// Different short cycles through divided streets can surround the same
/// physical island. Coincident kerbs must not emit two sets of wall faces.
/// Candidates arrive in order of increasing number of bounding roads.
pub(super) fn duplicate_bands(rings: &[Vec<Point>], tolerance: f64) -> Vec<usize> {
    let bounds: Vec<_> = rings
        .iter()
        .map(|r| {
            r.iter().fold(
                [
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ],
                |b, p| {
                    [
                        b[0].min(p[0]),
                        b[1].min(p[1]),
                        b[2].max(p[0]),
                        b[3].max(p[1]),
                    ]
                },
            )
        })
        .collect();
    let follows = |a: &[Point], b: &[Point]| {
        a.iter().all(|p| {
            b.iter()
                .zip(b.iter().cycle().skip(1))
                .any(|(u, v)| seg_dist(u[0], u[1], v[0], v[1], p[0], p[1]) <= tolerance)
        })
    };
    let mut kept: Vec<usize> = Vec::new();
    let mut duplicates = Vec::new();
    for (i, ring) in rings.iter().enumerate() {
        if kept.iter().any(|&j| {
            bounds[i]
                .iter()
                .zip(bounds[j])
                .all(|(a, b)| (a - b).abs() <= tolerance)
                && follows(ring, &rings[j])
                && follows(&rings[j], ring)
        }) {
            duplicates.push(i);
        } else {
            kept.push(i);
        }
    }
    duplicates
}
