//! Buildings against the drawn carriageways (buildings.road_clearance; the user's "houses blocking the road", GitHub
//! #523 #543 #1184 #433 #74): how far each footprint reaches into the carriageways (the road tiles' ground roads,
//! their per-segment half widths) and, for one that does, the footprint cut back to the carriageway's edge.
//!
//! A road is its centre line's segments with a half width each (tapers vary it along a road). A point's depth into the
//! carriageways is the most any segment's half width exceeds the point's distance to that segment (> 0: on the
//! asphalt). A footprint's depth is the most of its points' (its outline every `step` m and a grid inside it), its
//! overlap the grid's area on the asphalt.
//!
//! The cut (footprint_clear): while the footprint is deeper than `tol`, the deepest point's segment cuts it: the
//! half plane past that segment's edge (half width + `margin` from its centre line, on the footprint's own side: the
//! side holding more of it) is kept. A box (a pitched roof's walls: buildings.drawn_ring) keeps its shape: the side
//! facing the road moves in instead. A cut stands only when what it takes off is mostly carriageway (CUT_ROAD_SHARE of
//! it within ROAD_SLACK_M of one): a half plane runs on past its segment, and must not shave a wing round a corner.
//! Outcomes per footprint (status): CLEAR (no deeper than tol), TRIMMED (the cut ring), DEEP (it would lose more
//! than max_share or fall under MIN_AREA_M2), UNCLEAN (a cut not mostly road, or a box side not facing the road),
//! THROUGH (the road's centre line runs through it, MIN_SIDE_SHARE of it on each side: a passage, not a trim).
use crate::dm::Dm;

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

type Pt = [f64; 2];

pub const CLEAR: u8 = 0;
pub const TRIMMED: u8 = 1;
pub const DEEP: u8 = 2;
pub const UNCLEAN: u8 = 3;
pub const THROUGH: u8 = 4;

const BUCKET_M: f64 = 16.0;
const MAX_SAMPLES: f64 = 20000.0;     // grid samples inside a footprint at most (a bigger one samples coarser)
const MAX_CUTS: usize = 6;
const CUT_ROAD_SHARE: f64 = 0.6;
const ROAD_SLACK_M: f64 = 1.0;
const MIN_SIDE_SHARE: f64 = 0.2;
const THROUGH_RUN_M: f64 = 2.0;        // m of the centre line's segment inside a footprint for a road through it
const MIN_AREA_M2: f64 = 12.0;
const MIN_BOX_W: f64 = 2.5;
const BOX_FACING: f64 = 0.5;          // a box side moves in only when its outward normal is this much against the cut's

struct Segs {
    a: Vec<Pt>,
    b: Vec<Pt>,
    half: Vec<f64>,
    // bucket grid over the segments' boxes grown by their half widths
    x0: f64,
    y0: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
}

impl Segs {
    fn new(seg: &numpy::ndarray::ArrayView2<f64>, half: &numpy::ndarray::ArrayView1<f64>) -> Segs {
        let n = seg.nrows();
        let mut a = Vec::with_capacity(n);
        let mut b = Vec::with_capacity(n);
        let mut h = Vec::with_capacity(n);
        let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        for k in 0..n {
            let (p, q, w) = ([seg[[k, 0]], seg[[k, 1]]], [seg[[k, 2]], seg[[k, 3]]], half[k]);
            let ok = p.iter().chain(q.iter()).all(|v| v.is_finite()) && w.is_finite() && w > 0.0;
            a.push(p);
            b.push(q);
            h.push(if ok { w } else { 0.0 });
            if ok {
                for d in 0..2 {
                    lo[d] = lo[d].min(p[d].min(q[d]) - w);
                    hi[d] = hi[d].max(p[d].max(q[d]) + w);
                }
            }
        }
        if !lo[0].is_finite() {
            return Segs { a, b, half: h, x0: 0.0, y0: 0.0, nx: 0, ny: 0, cells: Vec::new() };
        }
        let nx = (((hi[0] - lo[0]) / BUCKET_M).floor() as usize + 1).min(4096);
        let ny = (((hi[1] - lo[1]) / BUCKET_M).floor() as usize + 1).min(4096);
        let mut s = Segs { a, b, half: h, x0: lo[0], y0: lo[1], nx, ny, cells: vec![Vec::new(); nx * ny] };
        for k in 0..n {
            let w = s.half[k];
            if w <= 0.0 {
                continue;
            }
            let (p, q) = (s.a[k], s.b[k]);
            let (i0, j0) = s.cell(p[0].min(q[0]) - w, p[1].min(q[1]) - w);
            let (i1, j1) = s.cell(p[0].max(q[0]) + w, p[1].max(q[1]) + w);
            for j in j0..=j1 {
                for i in i0..=i1 {
                    s.cells[j * nx + i].push(k as u32);
                }
            }
        }
        s
    }

    fn cell(&self, x: f64, y: f64) -> (usize, usize) {
        let i = ((x - self.x0) / BUCKET_M).floor().max(0.0) as usize;
        let j = ((y - self.y0) / BUCKET_M).floor().max(0.0) as usize;
        (i.min(self.nx.saturating_sub(1)), j.min(self.ny.saturating_sub(1)))
    }

    /// The segments whose grown boxes may reach the box lo..hi.
    fn near(&self, lo: Pt, hi: Pt) -> Vec<usize> {
        if self.nx == 0 || hi[0] < self.x0 || hi[1] < self.y0
            || lo[0] > self.x0 + self.nx as f64 * BUCKET_M || lo[1] > self.y0 + self.ny as f64 * BUCKET_M {
            return Vec::new();
        }
        let (i0, j0) = self.cell(lo[0], lo[1]);
        let (i1, j1) = self.cell(hi[0], hi[1]);
        let mut out: Vec<usize> = Vec::new();
        for j in j0..=j1 {
            for i in i0..=i1 {
                for &k in &self.cells[j * self.nx + i] {
                    let k = k as usize;
                    let (p, q, w) = (self.a[k], self.b[k], self.half[k]);
                    if p[0].min(q[0]) - w <= hi[0] && p[0].max(q[0]) + w >= lo[0]
                        && p[1].min(q[1]) - w <= hi[1] && p[1].max(q[1]) + w >= lo[1] {
                        out.push(k);
                    }
                }
            }
        }
        out.sort_unstable();
        out.dedup();
        out
    }

    /// (depth, segment) of point p against the segments ks: the most half - distance, NEG_INFINITY with none.
    fn depth(&self, p: Pt, ks: &[usize]) -> (f64, usize) {
        let mut best = (f64::NEG_INFINITY, usize::MAX);
        for &k in ks {
            let (a, b) = (self.a[k], self.b[k]);
            let d = [b[0] - a[0], b[1] - a[1]];
            let l2 = d[0] * d[0] + d[1] * d[1];
            let t = if l2 > 1e-12 { (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0) } else { 0.0 };
            let dist = (p[0] - a[0] - t * d[0]).dhypot(p[1] - a[1] - t * d[1]);
            let v = self.half[k] - dist;
            if v > best.0 {
                best = (v, k);
            }
        }
        best
    }
}

fn area2(p: &[Pt]) -> f64 {
    let n = p.len();
    let mut s = 0.0;
    for i in 0..n {
        let j = (i + 1) % n;
        s += p[i][0] * p[j][1] - p[j][0] * p[i][1];
    }
    s
}

fn bbox(p: &[Pt]) -> (Pt, Pt) {
    let mut lo = [f64::INFINITY; 2];
    let mut hi = [f64::NEG_INFINITY; 2];
    for q in p {
        for d in 0..2 {
            lo[d] = lo[d].min(q[d]);
            hi[d] = hi[d].max(q[d]);
        }
    }
    (lo, hi)
}

fn inside(p: &[Pt], x: f64, y: f64) -> bool {
    let n = p.len();
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (p[i][0], p[i][1], p[j][0], p[j][1]);
        if (yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi {
            c = !c;
        }
        j = i;
    }
    c
}

/// Grid samples (cell centres, `step` apart, coarser for a big footprint) inside ring p, and each one's cell area.
fn grid(p: &[Pt], step: f64) -> (Vec<Pt>, f64) {
    let (lo, hi) = bbox(p);
    let area = area2(p).abs() / 2.0;
    let s = step.max((area / MAX_SAMPLES).sqrt());
    let nx = ((hi[0] - lo[0]) / s).ceil().max(1.0) as usize;
    let ny = ((hi[1] - lo[1]) / s).ceil().max(1.0) as usize;
    let mut out = Vec::new();
    for j in 0..ny {
        let y = lo[1] + (j as f64 + 0.5) * s;
        for i in 0..nx {
            let x = lo[0] + (i as f64 + 0.5) * s;
            if inside(p, x, y) {
                out.push([x, y]);
            }
        }
    }
    (out, s * s)
}

/// The outline's points every `step` m (its corners among them).
fn outline(p: &[Pt], step: f64) -> Vec<Pt> {
    let n = p.len();
    let mut out = Vec::new();
    for i in 0..n {
        let (a, b) = (p[i], p[(i + 1) % n]);
        let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
        let k = ((l / step).ceil() as usize).max(1);
        for j in 0..k {
            let t = j as f64 / k as f64;
            out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
        }
    }
    out
}

/// (depth, deepest point, its segment, overlap m2) of ring p against the segments ks.
fn measure(p: &[Pt], s: &Segs, ks: &[usize], step: f64) -> (f64, Pt, usize, f64) {
    let mut best = (f64::NEG_INFINITY, [0.0, 0.0], usize::MAX);
    let (g, cell) = grid(p, step);
    let mut over = 0.0;
    for q in outline(p, step).into_iter().chain(g.iter().copied()) {
        let (d, k) = s.depth(q, ks);
        if d > best.0 {
            best = (d, q, k);
        }
    }
    for q in &g {
        if s.depth(*q, ks).0 > 0.0 {
            over += cell;
        }
    }
    (best.0, best.1, best.2, over)
}

/// Sutherland-Hodgman: the part of ring p where (x - o) . n >= c (a concave ring's pieces joined along the line).
fn clip_half(p: &[Pt], o: Pt, n: Pt, c: f64) -> Vec<Pt> {
    let f = |q: &Pt| (q[0] - o[0]) * n[0] + (q[1] - o[1]) * n[1] - c;
    let m = p.len();
    let mut out = Vec::with_capacity(m + 4);
    for i in 0..m {
        let (a, b) = (p[i], p[(i + 1) % m]);
        let (fa, fb) = (f(&a), f(&b));
        if fa >= 0.0 {
            out.push(a);
        }
        if (fa >= 0.0) != (fb >= 0.0) {
            let t = fa / (fa - fb);
            out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
        }
    }
    // (consecutive duplicates out)
    let mut clean: Vec<Pt> = Vec::with_capacity(out.len());
    for q in out {
        if clean.last().is_none_or(|l: &Pt| (l[0] - q[0]).dhypot(l[1] - q[1]) > 1e-6) {
            clean.push(q);
        }
    }
    while clean.len() > 1 && (clean[0][0] - clean[clean.len() - 1][0]).dhypot(clean[0][1] - clean[clean.len() - 1][1]) <= 1e-6 {
        clean.pop();
    }
    clean
}

/// m of segment a..b inside ring p (sampled every step m).
fn inside_run(p: &[Pt], a: Pt, b: Pt, step: f64) -> f64 {
    let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
    let k = ((l / step).ceil() as usize).max(1);
    (0..k).filter(|&j| {
        let t = (j as f64 + 0.5) / k as f64;
        inside(p, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    }).count() as f64 * l / k as f64
}

/// One footprint cleared: (status, ring, removed share, depth before, overlap m2 before).
fn clear_one(ring: &[Pt], boxed: bool, s: &Segs, tol: f64, margin: f64, max_share: f64, step: f64)
             -> (u8, Vec<Pt>, f64, f64, f64) {
    let mut p: Vec<Pt> = ring.to_vec();
    if p.len() < 3 {
        return (CLEAR, p, 0.0, f64::NEG_INFINITY, 0.0);
    }
    if area2(&p) < 0.0 {
        p.reverse();
    }
    let area0 = area2(&p) / 2.0;
    let reach = margin + ROAD_SLACK_M;
    let (lo, hi) = bbox(&p);
    let ks = s.near([lo[0] - reach, lo[1] - reach], [hi[0] + reach, hi[1] + reach]);
    if ks.is_empty() || area0 <= 0.0 {
        return (CLEAR, ring.to_vec(), 0.0, f64::NEG_INFINITY, 0.0);
    }
    let (depth0, mut q, mut k, over0) = measure(&p, s, &ks, step);
    let mut depth = depth0;
    let mut cuts = 0;
    while depth > tol {
        if cuts >= MAX_CUTS {
            return (DEEP, ring.to_vec(), 0.0, depth0, over0);
        }
        cuts += 1;
        let (a, b) = (s.a[k], s.b[k]);
        let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
        // the segment's normal (a degenerate one: away from the deepest point's foot)
        let n: Pt = if l > 1e-9 { [-(b[1] - a[1]) / l, (b[0] - a[0]) / l] } else {
            let d = (q[0] - a[0]).dhypot(q[1] - a[1]).max(1e-9);
            [(q[0] - a[0]) / d, (q[1] - a[1]) / d]
        };
        // the footprint's side of the centre line: the one holding more of it; a road through it is a passage
        let left = area2(&clip_half(&p, a, n, 0.0)).abs() / 2.0;
        let right = area2(&clip_half(&p, a, [-n[0], -n[1]], 0.0)).abs() / 2.0;
        let whole = area2(&p).abs() / 2.0;
        if left.min(right) > MIN_SIDE_SHARE * whole && inside_run(&p, a, b, step) >= THROUGH_RUN_M {
            return (THROUGH, ring.to_vec(), 0.0, depth0, over0);
        }
        let kn: Pt = if left >= right { n } else { [-n[0], -n[1]] };
        let c = s.half[k] + margin;
        let next: Vec<Pt> = if boxed && p.len() == 4 {
            // the box side facing the road moves in until both its corners are past the edge
            let mut best = (0.0f64, usize::MAX);
            for i in 0..4 {
                let (e0, e1) = (p[i], p[(i + 1) % 4]);
                let el = (e1[0] - e0[0]).dhypot(e1[1] - e0[1]).max(1e-9);
                let o = [(e1[1] - e0[1]) / el, -(e1[0] - e0[0]) / el];      // outward (counter-clockwise ring)
                let dot = o[0] * kn[0] + o[1] * kn[1];
                if dot < best.0 {
                    best = (dot, i);
                }
            }
            if best.1 == usize::MAX || -best.0 < BOX_FACING {
                return (UNCLEAN, ring.to_vec(), 0.0, depth0, over0);
            }
            let i = best.1;
            let (e0, e1) = (p[i], p[(i + 1) % 4]);
            let el = (e1[0] - e0[0]).dhypot(e1[1] - e0[1]);
            let o = [(e1[1] - e0[1]) / el, -(e1[0] - e0[0]) / el];
            let f = |x: &Pt| (x[0] - a[0]) * kn[0] + (x[1] - a[1]) * kn[1];
            let t = ((c - f(&e0)) / -best.0).max((c - f(&e1)) / -best.0).max(0.0);
            // the box's depth across that side (to the opposite side)
            let o2 = p[(i + 2) % 4];
            let across = (o2[0] - e0[0]) * -o[0] + (o2[1] - e0[1]) * -o[1];
            if across - t < MIN_BOX_W {
                return (DEEP, ring.to_vec(), 0.0, depth0, over0);
            }
            let mut nb = p.clone();
            nb[i] = [e0[0] - o[0] * t, e0[1] - o[1] * t];
            nb[(i + 1) % 4] = [e1[0] - o[0] * t, e1[1] - o[1] * t];
            nb
        } else {
            clip_half(&p, a, kn, c)
        };
        if next.len() < 3 {
            return (DEEP, ring.to_vec(), 0.0, depth0, over0);
        }
        let kept = area2(&next).abs() / 2.0;
        if kept < MIN_AREA_M2 || area0 - kept > max_share * area0 {
            return (DEEP, ring.to_vec(), 0.0, depth0, over0);
        }
        // what the cut takes off: mostly carriageway (within ROAD_SLACK_M of one)?
        let (g, _) = grid(&p, step);
        let (mut gone, mut road) = (0usize, 0usize);
        for x in &g {
            if !inside(&next, x[0], x[1]) {
                gone += 1;
                if s.depth(*x, &ks).0 > -(margin + ROAD_SLACK_M) {
                    road += 1;
                }
            }
        }
        if gone > 0 && (road as f64) < CUT_ROAD_SHARE * gone as f64 {
            return (UNCLEAN, ring.to_vec(), 0.0, depth0, over0);
        }
        p = next;
        let (d, nq, nk, _) = measure(&p, s, &ks, step);
        if gone == 0 && d >= depth - 1e-9 {
            return (UNCLEAN, ring.to_vec(), 0.0, depth0, over0);     // (no progress)
        }
        depth = d;
        q = nq;
        k = nk;
    }
    if cuts == 0 {
        return (CLEAR, ring.to_vec(), 0.0, depth0, over0);
    }
    let share = (area0 - area2(&p).abs() / 2.0) / area0;
    (TRIMMED, p, share, depth0, over0)
}

const MIN_PIECE_M2: f64 = 4.0;
const MAX_SPLITS: usize = 24;

/// The pieces of ring p off the carriageways (a passage's ground floor beside the road, buildings.road_clearance):
/// while a piece is deeper than tol into a carriageway, its deepest point's segment takes its carriageway's rectangle
/// (half width + margin either side of the segment, as far past its ends) out of it, the rest as up to four pieces
/// (beyond each edge, and past each end between them: a road ending at a facade leaves the building beyond its end
/// whole). Pieces under MIN_PIECE_M2 are dropped. None after MAX_SPLITS splits (a footprint a whole street network
/// crosses).
fn pieces_one(ring: &[Pt], s: &Segs, tol: f64, margin: f64, step: f64) -> Option<Vec<Vec<Pt>>> {
    let mut q0: Vec<Pt> = ring.to_vec();
    if q0.len() < 3 {
        return Some(Vec::new());
    }
    if area2(&q0) < 0.0 {
        q0.reverse();
    }
    let reach = margin + ROAD_SLACK_M;
    let mut queue = vec![q0];
    let mut out = Vec::new();
    let mut splits = 0;
    while let Some(q) = queue.pop() {
        if q.len() < 3 || area2(&q).abs() / 2.0 < MIN_PIECE_M2 {
            continue;
        }
        let (lo, hi) = bbox(&q);
        let ks = s.near([lo[0] - reach, lo[1] - reach], [hi[0] + reach, hi[1] + reach]);
        if ks.is_empty() {
            out.push(q);
            continue;
        }
        let (d, _, k, _) = measure(&q, s, &ks, step);
        if d <= tol || k == usize::MAX {
            out.push(q);
            continue;
        }
        splits += 1;
        if splits > MAX_SPLITS {
            return None;
        }
        let (a, b) = (s.a[k], s.b[k]);
        let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
        let u: Pt = if l > 1e-9 { [(b[0] - a[0]) / l, (b[1] - a[1]) / l] } else { [1.0, 0.0] };
        let n: Pt = [-u[1], u[0]];
        let c = s.half[k] + margin;
        let mid = clip_half(&clip_half(&q, a, [-n[0], -n[1]], -c), a, n, -c);
        for p in [clip_half(&q, a, n, c), clip_half(&q, a, [-n[0], -n[1]], c),
                  clip_half(&mid, a, [-u[0], -u[1]], c), clip_half(&mid, b, u, c)] {
            if p.len() >= 3 && area2(&p).abs() / 2.0 >= MIN_PIECE_M2 {
                queue.push(p);
            }
        }
    }
    Some(out)
}

/// footprint_pieces: each footprint's pieces off the carriageways (pieces_one; rings as footprint_road_overlap's):
/// (pieces xy (M, 2), piece off (P + 1), owner (P,): the footprint each piece is of, failed (R,): 1 where a footprint
/// was split more than MAX_SPLITS times, none of its pieces given).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn footprint_pieces<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                             seg: PyReadonlyArray2<'py, f64>, half: PyReadonlyArray1<'py, f64>, tol: f64, margin: f64,
                             step: f64)
                             -> PyResult<(Bound<'py, PyArray2<f64>>, Vec<i64>, Vec<i64>, Vec<u8>)> {
    let rings = rings_of(&xy.as_array(), &off.as_array())?;
    let s = segs_of(&seg.as_array(), &half.as_array())?;
    let step = if step > 0.05 { step } else { 0.5 };
    let (pts, offs, owner, failed) = py.detach(|| {
        let (mut pts, mut offs, mut owner, mut failed) = (Vec::new(), vec![0i64], Vec::new(), Vec::new());
        for (i, r) in rings.iter().enumerate() {
            match pieces_one(r, &s, tol, margin, step) {
                Some(ps) => {
                    failed.push(0u8);
                    for p in ps {
                        pts.extend(p);
                        offs.push(pts.len() as i64);
                        owner.push(i as i64);
                    }
                }
                None => failed.push(1u8),
            }
        }
        (pts, offs, owner, failed)
    });
    let mut out = Array2::<f64>::zeros((pts.len(), 2));
    for (i, p) in pts.iter().enumerate() {
        out[[i, 0]] = p[0];
        out[[i, 1]] = p[1];
    }
    Ok((out.into_pyarray(py), offs, owner, failed))
}

fn rings_of(xy: &numpy::ndarray::ArrayView2<f64>, off: &numpy::ndarray::ArrayView1<i64>) -> PyResult<Vec<Vec<Pt>>> {
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("bldclear: {m}"));
    if xy.ncols() < 2 || off.is_empty() {
        return Err(bad("xy (N, 2), off (R + 1)"));
    }
    let mut out = Vec::with_capacity(off.len() - 1);
    for r in 0..off.len() - 1 {
        let (i0, i1) = (off[r], off[r + 1]);
        if i0 < 0 || i1 < i0 || i1 as usize > xy.nrows() {
            return Err(bad("bad off"));
        }
        out.push((i0 as usize..i1 as usize).map(|i| [xy[[i, 0]], xy[[i, 1]]]).collect());
    }
    Ok(out)
}

fn segs_of(seg: &numpy::ndarray::ArrayView2<f64>, half: &numpy::ndarray::ArrayView1<f64>) -> PyResult<Segs> {
    if seg.ncols() < 4 || seg.nrows() != half.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("bldclear: seg (S, 4), half (S,)"));
    }
    Ok(Segs::new(seg, half))
}

/// Per footprint (rings xy one after another, off (R + 1), open: no closing point): (overlap m2 with the carriageways,
/// depth m into them: < 0 short of them, -inf with no road near, the half width of the road it is deepest into) against the road segments seg (S, 4: ax ay bx by)
/// of half widths half (S,), sampled every step m.
#[pyfunction]
pub fn footprint_road_overlap(py: Python<'_>, xy: PyReadonlyArray2<f64>, off: PyReadonlyArray1<i64>,
                              seg: PyReadonlyArray2<f64>, half: PyReadonlyArray1<f64>, step: f64)
                              -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
    let rings = rings_of(&xy.as_array(), &off.as_array())?;
    let s = segs_of(&seg.as_array(), &half.as_array())?;
    let step = if step > 0.05 { step } else { 0.5 };
    Ok(py.detach(|| {
        let mut over = Vec::with_capacity(rings.len());
        let mut depth = Vec::with_capacity(rings.len());
        let mut halfs = Vec::with_capacity(rings.len());
        for r in &rings {
            let (lo, hi) = if r.len() >= 3 { bbox(r) } else { ([0.0; 2], [0.0; 2]) };
            let ks = if r.len() >= 3 { s.near(lo, hi) } else { Vec::new() };
            if ks.is_empty() {
                over.push(0.0);
                depth.push(f64::NEG_INFINITY);
                halfs.push(f64::NAN);
                continue;
            }
            let (d, _, k, o) = measure(r, &s, &ks, step);
            over.push(o);
            depth.push(d);
            halfs.push(if k < s.half.len() { s.half[k] } else { f64::NAN });
        }
        (over, depth, halfs)
    }))
}

/// footprint_clear: each footprint (rings as footprint_road_overlap's; boxed (R,) 1 for a box kept a box) cut back
/// to the carriageways' edges (+ margin) where it is deeper than tol into them, taking at most max_share of it:
/// (rings out (M, 2), off (R + 1), status (R,) CLEAR / TRIMMED / DEEP / UNCLEAN / THROUGH, removed share (R,),
/// depth before (R,), overlap m2 before (R,)). A ring not TRIMMED comes back as it was given.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn footprint_clear<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                            boxed: PyReadonlyArray1<'py, u8>, seg: PyReadonlyArray2<'py, f64>,
                            half: PyReadonlyArray1<'py, f64>, tol: f64, margin: f64, max_share: f64, step: f64)
                            -> PyResult<(Bound<'py, PyArray2<f64>>, Vec<i64>, Vec<u8>, Vec<f64>, Vec<f64>, Vec<f64>)> {
    let rings = rings_of(&xy.as_array(), &off.as_array())?;
    let bx = boxed.as_array();
    if bx.len() != rings.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("bldclear: boxed (R,)"));
    }
    let bx: Vec<bool> = bx.iter().map(|&v| v != 0).collect();
    let s = segs_of(&seg.as_array(), &half.as_array())?;
    let step = if step > 0.05 { step } else { 0.5 };
    let (pts, offs, st, sh, dp, ov) = py.detach(|| {
        let (mut pts, mut offs, mut st, mut sh, mut dp, mut ov) = (Vec::new(), vec![0i64], Vec::new(), Vec::new(),
                                                                   Vec::new(), Vec::new());
        for (r, &b) in rings.iter().zip(bx.iter()) {
            let (code, ring, share, d, o) = clear_one(r, b, &s, tol, margin, max_share, step);
            pts.extend(ring);
            offs.push(pts.len() as i64);
            st.push(code);
            sh.push(share);
            dp.push(d);
            ov.push(o);
        }
        (pts, offs, st, sh, dp, ov)
    });
    let mut out = Array2::<f64>::zeros((pts.len(), 2));
    for (i, p) in pts.iter().enumerate() {
        out[[i, 0]] = p[0];
        out[[i, 1]] = p[1];
    }
    Ok((out.into_pyarray(py), offs, st, sh, dp, ov))
}

/// Twin footprints (buildings.footprint_twins): the same building mapped twice, one outline over the other (OSM's two
/// multipolygons of 75-76 Berners Street on the same outer ways, London: two walls in one plane, their facades
/// z-fighting, the user's report 2026-11-12). For each pair of rings whose bounding boxes meet, the share of each
/// ring's grid samples (`step` m, coarser for a big one: grid) inside the other; a pair each at least `share` inside
/// the other is a twin, and the lower ranked of the two is left out (rank: higher wins, then the lower index). Pairs
/// over MAX_TWIN_RATIO apart in area are never twins (a building and the site round it).
const MAX_TWIN_RATIO: f64 = 1.5;

fn twins(rings: &[Vec<Pt>], rank: &[f64], share: f64, step: f64) -> Vec<u8> {
    let n = rings.len();
    let boxes: Vec<(Pt, Pt)> = rings.iter().map(|r| bbox(r)).collect();
    let areas: Vec<f64> = rings.iter().map(|r| area2(r).abs() / 2.0).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| {
        boxes[a].0[0].partial_cmp(&boxes[b].0[0]).unwrap_or(std::cmp::Ordering::Equal).then(a.cmp(&b))
    });
    let mut grids: Vec<Option<Vec<Pt>>> = vec![None; n];
    let mut drop = vec![0u8; n];
    let inside_share = |x: usize, y: usize, grids: &mut Vec<Option<Vec<Pt>>>| -> f64 {
        if grids[x].is_none() {
            grids[x] = Some(grid(&rings[x], step).0);
        }
        let g = grids[x].as_ref().unwrap();
        if g.is_empty() {
            return 0.0;
        }
        let k = g.iter().filter(|q| inside(&rings[y], q[0], q[1])).count();
        k as f64 / g.len() as f64
    };
    for (oi, &a) in order.iter().enumerate() {
        for &b in &order[oi + 1..] {
            if boxes[b].0[0] > boxes[a].1[0] {
                break;
            }
            if boxes[b].0[1] > boxes[a].1[1] || boxes[b].1[1] < boxes[a].0[1] || drop[a] != 0 || drop[b] != 0 {
                continue;
            }
            let (sa, sb) = (areas[a], areas[b]);
            if sa < 1.0 || sb < 1.0 || sa.max(sb) > MAX_TWIN_RATIO * sa.min(sb) || rings[a].len() < 3
                || rings[b].len() < 3 {
                continue;
            }
            if inside_share(a, b, &mut grids) < share || inside_share(b, a, &mut grids) < share {
                continue;
            }
            let lose = if rank[a] > rank[b] || (rank[a] == rank[b] && a < b) { b } else { a };
            drop[lose] = 1;
        }
    }
    drop
}

/// footprint_twins: per ring (rings as footprint_road_overlap's), 1 where it is the lower ranked twin of another
/// (twins); rank (R,), share (0..1), step m.
#[pyfunction]
pub fn footprint_twins(py: Python<'_>, xy: PyReadonlyArray2<f64>, off: PyReadonlyArray1<i64>,
                       rank: PyReadonlyArray1<f64>, share: f64, step: f64) -> PyResult<Vec<u8>> {
    let rings = rings_of(&xy.as_array(), &off.as_array())?;
    let rank: Vec<f64> = rank.as_array().to_vec();
    if rank.len() != rings.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("bldclear: rank (R,)"));
    }
    let step = if step > 0.05 { step } else { 0.5 };
    Ok(py.detach(|| twins(&rings, &rank, share, step)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use numpy::ndarray::{arr1, arr2};

    fn segs(v: &[[f64; 4]], h: &[f64]) -> Segs {
        let a = arr2(v);
        let b = arr1(h);
        Segs::new(&a.view(), &b.view())
    }

    fn rect(x: f64, y: f64, w: f64, h: f64) -> Vec<Pt> {
        vec![[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    }

    #[test]
    fn edge_overhang_is_cut_to_the_edge() {
        // a road along y = 0, 8 m wide; a house from y = 2 to 12 (2 m into the asphalt)
        let s = segs(&[[-100.0, 0.0, 100.0, 0.0]], &[4.0]);
        let (st, ring, share, depth, over) = clear_one(&rect(0.0, 2.0, 10.0, 10.0), false, &s, 0.3, 0.25, 0.4, 0.5);
        assert_eq!(st, TRIMMED);
        assert!((depth - 2.0).abs() < 0.01, "{depth}");
        assert!((over - 20.0).abs() < 1.0, "{over}");
        assert!(ring.iter().all(|p| p[1] >= 4.25 - 1e-9));
        assert!((share - 0.225).abs() < 0.01, "{share}");
    }

    #[test]
    fn clear_and_deep_and_through() {
        let s = segs(&[[-100.0, 0.0, 100.0, 0.0]], &[4.0]);
        assert_eq!(clear_one(&rect(0.0, 4.2, 10.0, 10.0), false, &s, 0.3, 0.25, 0.4, 0.5).0, CLEAR);
        // 6 m of a 10 m deep house on the asphalt: more than max_share
        assert_eq!(clear_one(&rect(0.0, -2.0, 10.0, 10.0), false, &s, 0.3, 0.25, 0.4, 0.5).0, DEEP);
        // the centre line through the middle
        assert_eq!(clear_one(&rect(0.0, -6.0, 10.0, 12.0), false, &s, 0.3, 0.25, 0.4, 0.5).0, THROUGH);
    }

    #[test]
    fn a_box_stays_a_box() {
        let s = segs(&[[-100.0, 0.0, 100.0, 0.0]], &[4.0]);
        let (st, ring, _, _, _) = clear_one(&rect(0.0, 3.0, 10.0, 8.0), true, &s, 0.3, 0.25, 0.4, 0.5);
        assert_eq!(st, TRIMMED);
        assert_eq!(ring.len(), 4);
        let ys: Vec<f64> = ring.iter().map(|p| p[1]).collect();
        assert!(ys.iter().all(|&y| y >= 4.25 - 1e-9 && y <= 11.0 + 1e-9));
    }

    #[test]
    fn a_wing_past_the_roads_end_is_not_shaved() {
        // a short road ending at x = 0; an L-shaped building whose wing beyond the end reaches past the edge line
        let s = segs(&[[-50.0, 0.0, 0.0, 0.0]], &[4.0]);
        let l = vec![[-6.0, 3.0], [20.0, 3.0], [20.0, -10.0], [30.0, -10.0], [30.0, 13.0], [-6.0, 13.0]];
        let (st, _, _, _, _) = clear_one(&l, false, &s, 0.3, 0.25, 0.4, 0.5);
        assert_eq!(st, UNCLEAN);
    }

    #[test]
    fn pieces_beside_a_road_through_and_past_its_end() {
        // a road along y = 0 ending at x = 10, 8 m wide; a 30 x 20 block across it from x = 0 to 30
        let s = segs(&[[-100.0, 0.0, 10.0, 0.0]], &[4.0]);
        let ps = pieces_one(&rect(0.0, -10.0, 30.0, 20.0), &s, 0.3, 0.25, 0.5).unwrap();
        let area: f64 = ps.iter().map(|p| area2(p).abs() / 2.0).sum();
        // the road's rectangle out: x 0 .. 14.25, |y| < 4.25 (8.5 m x 14.25 m)
        assert!((area - (600.0 - 8.5 * 14.25)).abs() < 0.5, "{area}");
        for p in &ps {
            let (d, _, _, _) = measure(p, &s, &[0], 0.5);
            assert!(d <= 0.3, "{d}");
        }
        // clear of any road: itself
        let ps = pieces_one(&rect(0.0, 10.0, 10.0, 10.0), &s, 0.3, 0.25, 0.5).unwrap();
        assert_eq!(ps.len(), 1);
    }

    #[test]
    fn a_corner_house_is_cut_on_both_streets() {
        let s = segs(&[[-100.0, 0.0, 100.0, 0.0], [0.0, -100.0, 0.0, 100.0]], &[4.0, 4.0]);
        let (st, ring, _, _, _) = clear_one(&rect(3.0, 3.0, 10.0, 10.0), false, &s, 0.3, 0.25, 0.4, 0.5);
        assert_eq!(st, TRIMMED);
        assert!(ring.iter().all(|p| p[0] >= 4.25 - 1e-9 && p[1] >= 4.25 - 1e-9));
    }

    #[test]
    fn twin_footprints_one_kept() {
        let a = rect(0.0, 0.0, 40.0, 60.0);
        let b = vec![[0.2, 0.1], [40.1, 0.0], [40.0, 60.2], [0.0, 60.0]]; // the same outline again
        let c = rect(10.0, 10.0, 10.0, 10.0); // a smaller one inside: not a twin
        let d = rect(100.0, 0.0, 40.0, 60.0); // elsewhere
        let rings = vec![a.clone(), b.clone(), c, d];
        assert_eq!(twins(&rings, &[16.8, 25.8, 30.0, 10.0], 0.85, 0.5), vec![1, 0, 0, 0]);
        // equal rank: the lower index stays
        assert_eq!(twins(&[a.clone(), b], &[5.0, 5.0], 0.85, 0.5), vec![0, 1]);
        // half over each other: two buildings
        let e = rect(20.0, 0.0, 40.0, 60.0);
        assert_eq!(twins(&[a, e], &[1.0, 2.0], 0.85, 0.5), vec![0, 0]);
    }
}
