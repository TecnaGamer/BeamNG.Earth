//! Polyline helpers on plain slices, the same arithmetic as the kernels they stand for (roads.densify, clip_polyline,
//! offset_polyline, points_near_segments: bit for bit), for the tram code to call without numpy arrays.
use crate::dm::Dm;

pub type P2 = [f64; 2];

/// roads.densify: nodes added so no two are more than spacing apart in xy.
pub fn densify(p: &[P2], spacing: f64) -> Vec<P2> {
    let mut out: Vec<P2> = Vec::with_capacity(p.len() * 2);
    if let Some(f) = p.first() {
        out.push(*f);
    }
    for w in p.windows(2) {
        let (a, b) = (w[0], w[1]);
        let n = ((b[0] - a[0]).dhypot(b[1] - a[1]) / spacing).ceil() as i64;
        for s in 1..n {
            let f = s as f64 / n as f64;
            out.push([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]);
        }
        out.push(b);
    }
    out
}

/// roads.clip_polyline: the pieces of a polyline inside the box (Liang-Barsky per segment, consecutive pieces joined
/// within 1e-6).
pub fn clip_polyline(p: &[P2], x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<Vec<P2>> {
    let mut out: Vec<Vec<P2>> = Vec::new();
    let mut cur: Vec<P2> = Vec::new();
    let flush = |cur: &mut Vec<P2>, out: &mut Vec<Vec<P2>>| {
        if cur.len() >= 2 {
            out.push(std::mem::take(cur));
        }
        cur.clear();
    };
    for w in p.windows(2) {
        let (a, b) = (w[0], w[1]);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let (mut t0, mut t1, mut ok) = (0.0f64, 1.0f64, true);
        for (pp, q) in [(-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])] {
            if pp == 0.0 {
                if q < 0.0 {
                    ok = false;
                    break;
                }
            } else {
                let t = q / pp;
                if pp < 0.0 {
                    if t > t0 {
                        t0 = t;
                    }
                } else if t < t1 {
                    t1 = t;
                }
            }
        }
        if !ok || t0 > t1 {
            flush(&mut cur, &mut out);
            continue;
        }
        let pa = [a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0];
        let pb = [a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1];
        let joined = match cur.last() {
            None => false,
            Some(last) => {
                let mut m = 0.0f64;
                for c in 0..2 {
                    let v = (last[c] - pa[c]).abs();
                    if v.is_nan() {
                        m = f64::NAN;
                        break;
                    }
                    if v > m {
                        m = v;
                    }
                }
                !(m > 1e-6)
            }
        };
        if !joined {
            flush(&mut cur, &mut out);
            cur.push(pa);
        }
        cur.push(pb);
        if t1 < 1.0 {
            flush(&mut cur, &mut out);
        }
    }
    flush(&mut cur, &mut out);
    out
}

fn clip_np(x: f64, lo: f64, hi: f64) -> f64 {
    let x = if x.is_nan() || lo.is_nan() { f64::NAN } else if lo > x { lo } else { x };
    if x.is_nan() || hi.is_nan() { f64::NAN } else if hi < x { hi } else { x }
}

/// roads.offset_polyline with one distance for every point: the polyline moved d to its left, mitred joints (miter at
/// most 2x).
pub fn offset_polyline(p: &[P2], d: f64) -> Vec<P2> {
    let k = p.len();
    if k < 2 {
        return p.to_vec();
    }
    let nrm: Vec<P2> = (0..k - 1)
        .map(|i| {
            let (tx, ty) = (p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
            let mut l = tx.dhypot(ty);
            if l < 1e-9 {
                l = 1e-9;
            }
            let (tx, ty) = (tx / l, ty / l);
            [-ty, tx]
        })
        .collect();
    (0..k)
        .map(|i| {
            let v = if i == 0 {
                nrm[0]
            } else if i == k - 1 {
                nrm[k - 2]
            } else {
                let (a, b) = (nrm[i - 1], nrm[i]);
                let (mut mx, mut my) = (a[0] + b[0], a[1] + b[1]);
                let mut ml = mx.dhypot(my);
                if ml < 1e-9 {
                    ml = 1e-9;
                }
                mx /= ml;
                my /= ml;
                let c = clip_np(mx * b[0] + my * b[1], 0.5, 1.0);
                [mx / c, my / c]
            };
            [p[i][0] + v[0] * d, p[i][1] + v[1] * d]
        })
        .collect()
}

/// Carriageway segments a -> b with a half width: what a point lies "on" (fast.segments' table).
#[derive(Default, Clone)]
pub struct Segs {
    /// (ax, ay, abx, aby, max(|ab|^2, 1e-9), half width, box x0, y0, x1, y1)
    seg: Vec<[f64; 10]>,
    /// a tag per segment (0 none): what kind of road it is (near_tag)
    tag: Vec<u8>,
}

impl Segs {
    pub fn new(a: &[P2], b: &[P2], hw: &[f64]) -> Segs {
        let seg: Vec<[f64; 10]> = (0..a.len().min(b.len()).min(hw.len()))
            .map(|k| {
                let (ab0, ab1) = (b[k][0] - a[k][0], b[k][1] - a[k][1]);
                [a[k][0], a[k][1], ab0, ab1, (ab0 * ab0 + ab1 * ab1).max(1e-9), hw[k], a[k][0].min(b[k][0]),
                 a[k][1].min(b[k][1]), a[k][0].max(b[k][0]), a[k][1].max(b[k][1])]
            })
            .collect();
        let tag = vec![0u8; seg.len()];
        Segs { seg, tag }
    }

    /// The segments tagged (tags: one per segment, as given to new).
    pub fn with_tags(mut self, tags: &[u8]) -> Segs {
        if tags.len() == self.seg.len() {
            self.tag = tags.to_vec();
        }
        self
    }

    /// The tag of the nearest segment the point lies within the half width + gap of, 0 where it lies on none.
    pub fn near_tag(&self, p: P2, gap: f64) -> u8 {
        let (mut best, mut tag) = (f64::INFINITY, 0u8);
        for (k, g) in self.seg.iter().enumerate() {
            let r = g[5] + gap;
            if p[0] < g[6] - r || p[0] > g[8] + r || p[1] < g[7] - r || p[1] > g[9] + r {
                continue;
            }
            let t = (((p[0] - g[0]) * g[2] + (p[1] - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
            let d = (p[0] - (g[0] + t * g[2])).dhypot(p[1] - (g[1] + t * g[3]));
            if d < r && d < best {
                best = d;
                tag = self.tag[k];
            }
        }
        tag
    }

    pub fn is_empty(&self) -> bool {
        self.seg.is_empty()
    }

    /// roads._Segs.near: the point lies within a segment's half width + gap (points_near_segments).
    pub fn near(&self, p: P2, gap: f64) -> bool {
        self.seg.iter().any(|g| {
            let r = g[5] + gap;
            // (a point outside the segment's box grown by r is not within r of it: the exact test is only for the rest)
            if p[0] < g[6] - r || p[0] > g[8] + r || p[1] < g[7] - r || p[1] > g[9] + r {
                return false;
            }
            let t = (((p[0] - g[0]) * g[2] + (p[1] - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
            let d = (p[0] - (g[0] + t * g[2])).dhypot(p[1] - (g[1] + t * g[3]));
            d < r
        })
    }
}
