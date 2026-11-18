//! Stock props that fill mapped places (the user, 2026-11-10: "lets use as many assets as needed to give this world
//! detail and variety"): solar farms' panel tables, a container terminal's stacks, a trailer park's homes (area_grid),
//! the cranes along a port's quays (edge_spots) and the boats moored along a marina's piers (berths). Pure geometry in
//! the frame the server hands over; which places and which models is server/places.py's.
use crate::dm::Dm;

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::power::{in_rings, min_box};

type P2 = (f64, f64);

fn rings_of(pts: &numpy::ndarray::ArrayView2<'_, f64>, off: &numpy::ndarray::ArrayView1<'_, i64>) -> Vec<Vec<P2>> {
    (0..off.len().saturating_sub(1))
        .map(|k| (off[k].max(0) as usize..(off[k + 1].max(0) as usize).min(pts.nrows())).map(|i| (pts[[i, 0]], pts[[i, 1]])).collect())
        .filter(|r: &Vec<P2>| r.len() >= 3)
        .collect()
}

/// Polygons (each ring its own) with a bucket index of their boxes.
struct Polys {
    rings: Vec<Vec<P2>>,
    boxes: Vec<[f64; 4]>,
    cell: f64,
    index: HashMap<(i64, i64), Vec<u32>>,
}

impl Polys {
    fn new(rings: Vec<Vec<P2>>, cell: f64) -> Polys {
        let mut boxes = Vec::with_capacity(rings.len());
        let mut index: HashMap<(i64, i64), Vec<u32>> = HashMap::new();
        for (k, r) in rings.iter().enumerate() {
            let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
            for &(x, y) in r {
                b = [b[0].min(x), b[1].min(y), b[2].max(x), b[3].max(y)];
            }
            boxes.push(b);
            let (i0, j0, i1, j1) = ((b[0] / cell).floor() as i64, (b[1] / cell).floor() as i64,
                                    (b[2] / cell).floor() as i64, (b[3] / cell).floor() as i64);
            if (i1 - i0 + 1) * (j1 - j0 + 1) > 4096 {
                index.entry((i64::MIN, i64::MIN)).or_default().push(k as u32);    // (huge: always looked at)
                continue;
            }
            for i in i0..=i1 {
                for j in j0..=j1 {
                    index.entry((i, j)).or_default().push(k as u32);
                }
            }
        }
        Polys { rings, boxes, cell, index }
    }

    /// The polygons whose boxes may reach the box b.
    fn near(&self, b: [f64; 4]) -> Vec<usize> {
        let mut out: Vec<usize> = self.index.get(&(i64::MIN, i64::MIN)).map(|v| v.iter().map(|&k| k as usize).collect())
            .unwrap_or_default();
        let (i0, j0, i1, j1) = ((b[0] / self.cell).floor() as i64, (b[1] / self.cell).floor() as i64,
                                (b[2] / self.cell).floor() as i64, (b[3] / self.cell).floor() as i64);
        for i in i0..=i1 {
            for j in j0..=j1 {
                if let Some(v) = self.index.get(&(i, j)) {
                    out.extend(v.iter().map(|&k| k as usize));
                }
            }
        }
        out.sort_unstable();
        out.dedup();
        out.retain(|&k| {
            let q = self.boxes[k];
            q[0] <= b[2] && q[2] >= b[0] && q[1] <= b[3] && q[3] >= b[1]
        });
        out
    }

    fn contains(&self, k: usize, x: f64, y: f64) -> bool {
        in_rings(std::slice::from_ref(&self.rings[k]), x, y)
    }
}

/// Segments (ax, ay, bx, by, half width) with a bucket index.
struct Segs {
    s: Vec<[f64; 5]>,
    cell: f64,
    index: HashMap<(i64, i64), Vec<u32>>,
}

impl Segs {
    fn new(arr: &numpy::ndarray::ArrayView2<'_, f64>, cell: f64) -> Segs {
        let mut s = Vec::new();
        let mut index: HashMap<(i64, i64), Vec<u32>> = HashMap::new();
        if arr.ncols() >= 5 {
            for r in 0..arr.nrows() {
                let q = [arr[[r, 0]], arr[[r, 1]], arr[[r, 2]], arr[[r, 3]], arr[[r, 4]]];
                if !q.iter().all(|v| v.is_finite()) {
                    continue;
                }
                let k = s.len() as u32;
                let (x0, x1) = (q[0].min(q[2]) - q[4], q[0].max(q[2]) + q[4]);
                let (y0, y1) = (q[1].min(q[3]) - q[4], q[1].max(q[3]) + q[4]);
                let (i0, j0, i1, j1) = ((x0 / cell).floor() as i64, (y0 / cell).floor() as i64,
                                        (x1 / cell).floor() as i64, (y1 / cell).floor() as i64);
                if (i1 - i0 + 1) * (j1 - j0 + 1) <= 4096 {
                    for i in i0..=i1 {
                        for j in j0..=j1 {
                            index.entry((i, j)).or_default().push(k);
                        }
                    }
                } else {
                    index.entry((i64::MIN, i64::MIN)).or_default().push(k);
                }
                s.push(q);
            }
        }
        Segs { s, cell, index }
    }

    fn near(&self, b: [f64; 4]) -> Vec<usize> {
        let mut out: Vec<usize> = self.index.get(&(i64::MIN, i64::MIN)).map(|v| v.iter().map(|&k| k as usize).collect())
            .unwrap_or_default();
        let (i0, j0, i1, j1) = ((b[0] / self.cell).floor() as i64, (b[1] / self.cell).floor() as i64,
                                (b[2] / self.cell).floor() as i64, (b[3] / self.cell).floor() as i64);
        for i in i0..=i1 {
            for j in j0..=j1 {
                if let Some(v) = self.index.get(&(i, j)) {
                    out.extend(v.iter().map(|&k| k as usize));
                }
            }
        }
        out.sort_unstable();
        out.dedup();
        out
    }
}

/// A rectangle: centre, unit axis u (its length a along u, its width b across).
#[derive(Clone, Copy)]
struct Rect {
    c: P2,
    u: P2,
    ha: f64,
    hb: f64,
}

impl Rect {
    fn corners(&self) -> [P2; 4] {
        let v = (-self.u.1, self.u.0);
        let at = |s: f64, t: f64| (self.c.0 + s * self.ha * self.u.0 + t * self.hb * v.0,
                                   self.c.1 + s * self.ha * self.u.1 + t * self.hb * v.1);
        [at(-1.0, -1.0), at(1.0, -1.0), at(1.0, 1.0), at(-1.0, 1.0)]
    }

    fn bbox(&self, grow: f64) -> [f64; 4] {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for (x, y) in self.corners() {
            b = [b[0].min(x), b[1].min(y), b[2].max(x), b[3].max(y)];
        }
        [b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow]
    }

    /// Points over the rectangle grown by g, at most `step` apart (its outline and inside).
    fn samples(&self, g: f64, step: f64) -> Vec<P2> {
        let (ha, hb) = (self.ha + g, self.hb + g);
        let na = ((2.0 * ha / step).ceil() as usize).max(1);
        let nb = ((2.0 * hb / step).ceil() as usize).max(1);
        let v = (-self.u.1, self.u.0);
        let mut out = Vec::with_capacity((na + 1) * (nb + 1));
        for i in 0..=na {
            for j in 0..=nb {
                let (s, t) = (-ha + 2.0 * ha * i as f64 / na as f64, -hb + 2.0 * hb * j as f64 / nb as f64);
                out.push((self.c.0 + s * self.u.0 + t * v.0, self.c.1 + s * self.u.1 + t * v.1));
            }
        }
        out
    }

    fn contains(&self, p: P2) -> bool {
        let (dx, dy) = (p.0 - self.c.0, p.1 - self.c.1);
        let (a, b) = (dx * self.u.0 + dy * self.u.1, -dx * self.u.1 + dy * self.u.0);
        a.abs() <= self.ha && b.abs() <= self.hb
    }

    fn overlaps(&self, o: &Rect) -> bool {
        let axes = [self.u, (-self.u.1, self.u.0), o.u, (-o.u.1, o.u.0)];
        let d = (o.c.0 - self.c.0, o.c.1 - self.c.1);
        let reach = |r: &Rect, ax: P2| {
            let v = (-r.u.1, r.u.0);
            r.ha * (r.u.0 * ax.0 + r.u.1 * ax.1).abs() + r.hb * (v.0 * ax.0 + v.1 * ax.1).abs()
        };
        axes.iter().all(|&ax| (d.0 * ax.0 + d.1 * ax.1).abs() < reach(self, ax) + reach(o, ax))
    }
}

fn seg_point(p: P2, a: P2, b: P2) -> f64 {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let l2 = dx * dx + dy * dy;
    let t = if l2 > 1e-12 { (((p.0 - a.0) * dx + (p.1 - a.1) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
    (p.0 - a.0 - t * dx).dhypot(p.1 - a.1 - t * dy)
}

fn cross(o: P2, a: P2, b: P2) -> f64 {
    (a.0 - o.0) * (b.1 - o.1) - (a.1 - o.1) * (b.0 - o.0)
}

fn segs_cross(a: P2, b: P2, c: P2, d: P2) -> bool {
    let (d1, d2, d3, d4) = (cross(c, d, a), cross(c, d, b), cross(a, b, c), cross(a, b, d));
    (d1 > 0.0) != (d2 > 0.0) && (d3 > 0.0) != (d4 > 0.0)
}

/// Distance from segment a-b to the rectangle (0 when they touch).
fn rect_seg(r: &Rect, a: P2, b: P2) -> f64 {
    if r.contains(a) || r.contains(b) {
        return 0.0;
    }
    let c = r.corners();
    let mut best = f64::INFINITY;
    for i in 0..4 {
        let (p, q) = (c[i], c[(i + 1) % 4]);
        if segs_cross(a, b, p, q) {
            return 0.0;
        }
        best = best.min(seg_point(p, a, b)).min(seg_point(a, p, q)).min(seg_point(b, p, q));
    }
    best
}

/// Is the rectangle clear of the segments (each its half width away: the caller's clearance in it) and of the
/// polygons (clear away)?
fn clear_of(r: &Rect, segs: &Segs, polys: &Polys, clear: f64) -> bool {
    let bb = r.bbox(clear);
    for k in segs.near(r.bbox(0.0)) {
        let q = segs.s[k];
        if rect_seg(r, (q[0], q[1]), (q[2], q[3])) < q[4] {
            return false;
        }
    }
    let near = polys.near(bb);
    if near.is_empty() {
        return true;
    }
    let grown = Rect { ha: r.ha + clear, hb: r.hb + clear, ..*r };
    for k in near {
        let ring = &polys.rings[k];
        if ring.iter().any(|&p| grown.contains(p)) {
            return false;
        }
        if grown.samples(0.0, 2.0).iter().any(|&(x, y)| polys.contains(k, x, y)) {
            return false;
        }
    }
    true
}

/// Items on a grid filling an area (pts (n, 2) and ring offsets (k + 1): its rings, even-odd): each a rectangle
/// size [a, b] (a along the grid's axis), centres pitch [a, b] apart, with an aisle [a, b] more after every block
/// [a, b] items (0: none), the first one margin inside the area's bounding rectangle. The axis: axis_deg (degrees
/// counter-clockwise from +x), NaN for the longer side of the smallest rectangle around the area's largest ring. An item
/// is kept when its rectangle, margin larger, lies inside the area, it is clear of the segments seg (m, 5) [ax, ay,
/// bx, by, half width: the clearance kept from its line], `clear` off the polygons avoid (pts (q, 2), ring offsets: each ring its own), and its centre is in the
/// box bx [x0, y0, x1, y1) (every tile lays the same grid and keeps its own). Only the grid cells that reach the box are
/// looked at (a 20 km² farm costs a tile its own share). Returns (n, 5) [x, y, yaw degrees of the axis, i, j].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn area_grid<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                      axis_deg: f64, size: [f64; 2], pitch: [f64; 2], block: [i64; 2], aisle: [f64; 2], margin: f64,
                      seg: PyReadonlyArray2<'py, f64>, avoid_pts: PyReadonlyArray2<'py, f64>,
                      avoid_off: PyReadonlyArray1<'py, i64>, clear: f64, bx: [f64; 4]) -> Bound<'py, PyArray2<f64>> {
    let rings = rings_of(&pts.as_array(), &ring_off.as_array());
    let segs = Segs::new(&seg.as_array(), 32.0);
    let polys = Polys::new(rings_of(&avoid_pts.as_array(), &avoid_off.as_array()), 32.0);
    let out = py.detach(|| grid_core(&rings, axis_deg, size, pitch, block, aisle, margin, &segs, &polys, clear, bx));
    let n = out.len() / 5;
    numpy::ndarray::Array2::from_shape_vec((n, 5), out).unwrap().into_pyarray(py)
}

#[allow(clippy::too_many_arguments)]
fn grid_core(rings: &[Vec<P2>], axis_deg: f64, size: [f64; 2], pitch: [f64; 2], block: [i64; 2], aisle: [f64; 2],
             margin: f64, segs: &Segs, polys: &Polys, clear: f64, bx: [f64; 4]) -> Vec<f64> {
    let mut out = Vec::new();
    if rings.is_empty() || pitch[0] <= 0.0 || pitch[1] <= 0.0 {
        return out;
    }
    let (u, ar, br) = if axis_deg.is_finite() {
        let u = (axis_deg.to_radians().dcos(), axis_deg.to_radians().dsin());
        let v = (-u.1, u.0);
        let (mut a, mut b) = ((f64::INFINITY, f64::NEG_INFINITY), (f64::INFINITY, f64::NEG_INFINITY));
        for r in rings {
            for &(x, y) in r {
                let (pa, pb) = (x * u.0 + y * u.1, x * v.0 + y * v.1);
                a = (a.0.min(pa), a.1.max(pa));
                b = (b.0.min(pb), b.1.max(pb));
            }
        }
        (u, a, b)
    } else {
        let big = rings.iter().max_by(|a, b| {
            crate::power::ring_area(a).partial_cmp(&crate::power::ring_area(b)).unwrap_or(std::cmp::Ordering::Equal)
        }).unwrap();
        let Some((u, v, a, b)) = min_box(big) else { return out };
        if a.1 - a.0 >= b.1 - b.0 {
            // (the other rings, if any, inside the same range or cut by it)
            (u, a, b)
        } else {
            (v, b, (-a.1, -a.0))
        }
    };
    let v = (-u.1, u.0);
    let yaw = u.1.datan2(u.0).to_degrees().rem_euclid(360.0);
    // the box's corners in (a, b): which grid cells reach it
    let (mut ba, mut bb) = ((f64::INFINITY, f64::NEG_INFINITY), (f64::INFINITY, f64::NEG_INFINITY));
    for (x, y) in [(bx[0], bx[1]), (bx[2], bx[1]), (bx[2], bx[3]), (bx[0], bx[3])] {
        let (pa, pb) = (x * u.0 + y * u.1, x * v.0 + y * v.1);
        ba = (ba.0.min(pa), ba.1.max(pa));
        bb = (bb.0.min(pb), bb.1.max(pb));
    }
    // (a hair more than the margin: the first item's samples off the boundary itself)
    let lo = [ar.0 + margin + size[0] / 2.0 + 0.01, br.0 + margin + size[1] / 2.0 + 0.01];
    let hi = [ar.1, br.1];
    let pos = |axis: usize, i: i64| -> f64 {
        let blk = block[axis];
        let extra = if blk > 0 { (i.div_euclid(blk)) as f64 * aisle[axis] } else { 0.0 };
        lo[axis] + i as f64 * pitch[axis] + extra
    };
    let range = |axis: usize, from: f64, to: f64| -> (i64, i64) {
        let blk = block[axis];
        let period = if blk > 0 { blk as f64 * pitch[axis] + aisle[axis] } else { pitch[axis] };
        let per = if blk > 0 { blk as f64 } else { 1.0 };
        let n_max = (((hi[axis] - lo[axis]) / pitch[axis]).floor() as i64).max(-1);
        let i0 = (((from - lo[axis]) / period).floor() * per) as i64 - 2;
        let i1 = (((to - lo[axis]) / period).ceil() * per) as i64 + 2;
        (i0.max(0), i1.min(n_max))
    };
    let (ia, ja) = range(0, ba.0 - size[0], ba.1 + size[0]);
    let (ib, jb) = range(1, bb.0 - size[1], bb.1 + size[1]);
    if ja < ia || jb < ib || (ja - ia + 1) * (jb - ib + 1) > 4_000_000 {
        return out;
    }
    for i in ia..=ja {
        let a = pos(0, i);
        if a + size[0] / 2.0 > hi[0] - margin + 1e-9 {
            break;
        }
        for j in ib..=jb {
            let b = pos(1, j);
            if b + size[1] / 2.0 > hi[1] - margin + 1e-9 {
                break;
            }
            let (x, y) = (a * u.0 + b * v.0, a * u.1 + b * v.1);
            if !(x >= bx[0] && x < bx[2] && y >= bx[1] && y < bx[3]) {
                continue;
            }
            let r = Rect { c: (x, y), u, ha: size[0] / 2.0, hb: size[1] / 2.0 };
            if !r.samples(margin, 2.5).iter().all(|&(px, py)| in_rings(rings, px, py)) {
                continue;
            }
            if !clear_of(&r, segs, polys, clear) {
                continue;
            }
            out.extend_from_slice(&[x, y, yaw, i as f64, j as f64]);
        }
    }
    out
}

/// Spots along an area's rings where it meets water (the cranes along a port's quays): every `spacing` m along each
/// ring of pts / ring_off (from its first point), a spot where the point `probe` m out one side is in water (any
/// ring of water_pts / water_off) and `probe` m the other side is not; the spot `inset` m back from the edge, its yaw
/// (degrees) toward the water. Only spots in the box bx. Returns (n, 3) [x, y, yaw].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn edge_spots<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                       water_pts: PyReadonlyArray2<'py, f64>, water_off: PyReadonlyArray1<'py, i64>, spacing: f64,
                       probe: f64, inset: f64, bx: [f64; 4]) -> Bound<'py, PyArray2<f64>> {
    let rings = rings_of(&pts.as_array(), &ring_off.as_array());
    let water = Polys::new(rings_of(&water_pts.as_array(), &water_off.as_array()), 64.0);
    let wet = |x: f64, y: f64| water.near([x, y, x, y]).into_iter().any(|k| water.contains(k, x, y));
    let mut out = Vec::new();
    if spacing > 0.0 {
        for r in &rings {
            let n = r.len();
            let mut s0 = 0.0;
            let mut next = spacing / 2.0;
            for i in 0..n {
                let (p, q) = (r[i], r[(i + 1) % n]);
                let l = (q.0 - p.0).dhypot(q.1 - p.1);
                if l < 1e-9 {
                    continue;
                }
                let t = ((q.0 - p.0) / l, (q.1 - p.1) / l);
                while next <= s0 + l {
                    let f = next - s0;
                    let c = (p.0 + t.0 * f, p.1 + t.1 * f);
                    for nrm in [(-t.1, t.0), (t.1, -t.0)] {
                        if wet(c.0 + nrm.0 * probe, c.1 + nrm.1 * probe) && !wet(c.0 - nrm.0 * probe, c.1 - nrm.1 * probe) {
                            let (x, y) = (c.0 - nrm.0 * inset, c.1 - nrm.1 * inset);
                            if x >= bx[0] && x < bx[2] && y >= bx[1] && y < bx[3] {
                                out.extend_from_slice(&[x, y, nrm.1.datan2(nrm.0).to_degrees().rem_euclid(360.0)]);
                            }
                            break;
                        }
                    }
                    next += spacing;
                }
                s0 += l;
            }
        }
    }
    let k = out.len() / 3;
    numpy::ndarray::Array2::from_shape_vec((k, 3), out).unwrap().into_pyarray(py)
}

/// Boats moored along piers: along each line of lines / line_off (half widths line_hw, keys line_key: a number per
/// line that orders boats where two would overlap) a boat size [length, beam] every length + gap m from its first
/// point, on both sides, beam / 2 + `off` m off the pier's edge, its length along the pier. A boat is kept when its
/// footprint lies in water (water_pts / water_off, any ring), its centre in one of the keep rings (none given: anywhere),
/// it is clear (clear m) of every segment of seg (m, 5) [ax, ay, bx, by, half width] but its own pier's, and no boat
/// with a lower key overlaps it. Only boats with their centres in bx. Returns (n, 4) [x, y, yaw degrees, key].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn berths<'py>(py: Python<'py>, lines: PyReadonlyArray2<'py, f64>, line_off: PyReadonlyArray1<'py, i64>,
                   line_hw: PyReadonlyArray1<'py, f64>, line_key: PyReadonlyArray1<'py, f64>, size: [f64; 2], gap: f64,
                   off: f64, water_pts: PyReadonlyArray2<'py, f64>, water_off: PyReadonlyArray1<'py, i64>,
                   keep_pts: PyReadonlyArray2<'py, f64>, keep_off: PyReadonlyArray1<'py, i64>,
                   seg: PyReadonlyArray2<'py, f64>, clear: f64, bx: [f64; 4]) -> Bound<'py, PyArray2<f64>> {
    let (lp, lo, hw, keys) = (lines.as_array(), line_off.as_array(), line_hw.as_array(), line_key.as_array());
    let water = Polys::new(rings_of(&water_pts.as_array(), &water_off.as_array()), 64.0);
    let keep = Polys::new(rings_of(&keep_pts.as_array(), &keep_off.as_array()), 64.0);
    let segs = Segs::new(&seg.as_array(), 16.0);
    let wet = |x: f64, y: f64| water.near([x, y, x, y]).into_iter().any(|k| water.contains(k, x, y));
    let kept = |x: f64, y: f64| keep.rings.is_empty() || keep.near([x, y, x, y]).into_iter().any(|k| keep.contains(k, x, y));
    // candidates: (rect, key, line). Two ways each side: in a slip (the boat's bow to the pier, beam + gap apart: a
    // main pier's berths, keys first) and alongside (its length along the pier: a finger's, or a quay's)
    let mut cand: Vec<(Rect, f64, usize)> = Vec::new();
    let mut closed_rings: Vec<Vec<P2>> = Vec::new();
    for li in 0..lo.len().saturating_sub(1) {
        let (a0, a1) = (lo[li].max(0) as usize, (lo[li + 1].max(0) as usize).min(lp.nrows()));
        if a1 < a0 + 2 {
            continue;
        }
        let first = (lp[[a0, 0]], lp[[a0, 1]]);
        let last = (lp[[a1 - 1, 0]], lp[[a1 - 1, 1]]);
        if a1 >= a0 + 4 && (first.0 - last.0).dhypot(first.1 - last.1) < 1e-6 {
            closed_rings.push((a0..a1 - 1).map(|i| (lp[[i, 0]], lp[[i, 1]])).collect());
        }
        let h = if li < hw.len() { hw[li] } else { 1.0 };
        let key = if li < keys.len() { keys[li] } else { li as f64 };
        for (mode, (along, across)) in [(size[1], size[0]), (size[0], size[1])].into_iter().enumerate() {
            let step = along + gap;
            if step <= 0.0 {
                continue;
            }
            let mut s0 = 0.0;
            let mut next = gap + along / 2.0;
            let mut k = 0usize;
            for i in a0..a1 - 1 {
                let (p, q) = ((lp[[i, 0]], lp[[i, 1]]), (lp[[i + 1, 0]], lp[[i + 1, 1]]));
                let l = (q.0 - p.0).dhypot(q.1 - p.1);
                if l < 1e-9 {
                    continue;
                }
                let t = ((q.0 - p.0) / l, (q.1 - p.1) / l);
                // (a boat stays on one straight piece of its pier: one that would reach past a bend is left out)
                while next + along / 2.0 <= s0 + l + 1e-9 {
                    if next - along / 2.0 >= s0 - 1e-9 {
                        let f = next - s0;
                        let c = (p.0 + t.0 * f, p.1 + t.1 * f);
                        for (si, side) in [1.0f64, -1.0].iter().enumerate() {
                            let d = h + off + across / 2.0;
                            let n = (-t.1 * side, t.0 * side);
                            let cc = (c.0 + n.0 * d, c.1 + n.1 * d);
                            let u = if mode == 0 { n } else { t };
                            cand.push((Rect { c: cc, u, ha: size[0] / 2.0, hb: size[1] / 2.0 },
                                       key + mode as f64 * 10.0 + (2 * k + si) as f64 * 1e-6, li));
                        }
                    }
                    k += 1;
                    next += step;
                }
                s0 += l;
            }
        }
    }
    // each one on its own: in water, by a keep area, out of the pier outlines, clear of the other piers
    let closed = Polys::new(closed_rings, 32.0);
    let mut ok: Vec<(Rect, f64)> = Vec::new();
    for (r, key, _li) in cand {
        if !r.samples(0.0, 1.5).iter().all(|&(x, y)| wet(x, y)) || !kept(r.c.0, r.c.1) {
            continue;
        }
        if closed.near(r.bbox(0.0)).into_iter().any(|k| r.corners().iter().chain(std::iter::once(&r.c))
            .any(|&(x, y)| closed.contains(k, x, y))) {
            continue;
        }
        let mut free = true;
        for k in segs.near(r.bbox(clear + 20.0)) {
            let q = segs.s[k];
            if rect_seg(&r, (q[0], q[1]), (q[2], q[3])) < q[4] + clear - 1e-6 {
                free = false;
                break;
            }
        }
        if free {
            ok.push((r, key));
        }
    }
    // overlapping boats: the lower key stays (the same choice in every tile that sees both)
    ok.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut placed: Vec<(Rect, f64)> = Vec::new();
    let cell = 16.0;
    let mut out = Vec::new();
    for (r, key) in ok {
        let b = r.bbox(0.0);
        let (i0, j0, i1, j1) = ((b[0] / cell).floor() as i64, (b[1] / cell).floor() as i64,
                                (b[2] / cell).floor() as i64, (b[3] / cell).floor() as i64);
        let mut hit = false;
        'outer: for i in i0..=i1 {
            for j in j0..=j1 {
                if let Some(v) = grid.get(&(i, j)) {
                    if v.iter().any(|&m| placed[m].0.overlaps(&r)) {
                        hit = true;
                        break 'outer;
                    }
                }
            }
        }
        if hit {
            continue;
        }
        let m = placed.len();
        placed.push((r, key));
        for i in i0..=i1 {
            for j in j0..=j1 {
                grid.entry((i, j)).or_default().push(m);
            }
        }
        if r.c.0 >= bx[0] && r.c.0 < bx[2] && r.c.1 >= bx[1] && r.c.1 < bx[3] {
            out.extend_from_slice(&[r.c.0, r.c.1, r.u.1.datan2(r.u.0).to_degrees().rem_euclid(360.0), key]);
        }
    }
    let k = out.len() / 4;
    numpy::ndarray::Array2::from_shape_vec((k, 4), out).unwrap().into_pyarray(py)
}

/// Fence panels around an area (a construction site's hoarding): along every ring of pts / ring_off, panels `length`
/// long end to end from the ring's first point, each given at its forward end with its yaw (degrees) along the ring
/// (the stock chain-link panel's origin is its forward end, its body along local -x), the last one of a side
/// overlapping its corner; a panel whose middle is within `gate` m of a segment of seg (m, 5) [ax, ay, bx, by, half
/// width] is left out (a road into the site). Only panels whose middles are in bx. Returns (n, 3) [x, y, yaw].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn outline_panels<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                           length: f64, seg: PyReadonlyArray2<'py, f64>, gate: f64, bx: [f64; 4])
                           -> Bound<'py, PyArray2<f64>> {
    let rings = rings_of(&pts.as_array(), &ring_off.as_array());
    let segs = Segs::new(&seg.as_array(), 32.0);
    let mut out = Vec::new();
    if length > 0.0 {
        for r in &rings {
            let n = r.len();
            for i in 0..n {
                let (p, q) = (r[i], r[(i + 1) % n]);
                let l = (q.0 - p.0).dhypot(q.1 - p.1);
                if l < 1e-6 {
                    continue;
                }
                let (dx, dy) = ((q.0 - p.0) / l, (q.1 - p.1) / l);
                let yaw = dy.datan2(dx).to_degrees().rem_euclid(360.0);
                let m = (l / length).ceil() as usize;
                for s in 1..=m {
                    let t = (s as f64 * length).min(l.max(length));
                    let (x, y) = (p.0 + dx * t, p.1 + dy * t);
                    let (mx, my) = (x - dx * length / 2.0, y - dy * length / 2.0);
                    if !(mx >= bx[0] && mx < bx[2] && my >= bx[1] && my < bx[3]) {
                        continue;
                    }
                    let gated = segs.near([mx - gate, my - gate, mx + gate, my + gate]).into_iter().any(|k| {
                        let q = segs.s[k];
                        seg_point((mx, my), (q[0], q[1]), (q[2], q[3])) < q[4] + gate
                    });
                    if !gated {
                        out.extend_from_slice(&[x, y, yaw]);
                    }
                }
            }
        }
    }
    let k = out.len() / 3;
    numpy::ndarray::Array2::from_shape_vec((k, 3), out).unwrap().into_pyarray(py)
}

/// Marks along a line where it crosses the frame's grid lines `spacing` apart (x = k * spacing on a piece running more
/// along x, y = k * spacing on one more along y): a tunnel's jet fans, the same places whichever tile clips the line
/// (each piece of it gives the marks on that piece). pts (k, 3). Returns (m, 5) [x, y, z, unit tangent x, y].
#[pyfunction]
pub fn grid_marks<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, spacing: f64) -> Bound<'py, PyArray2<f64>> {
    let p = pts.as_array();
    let mut out = Vec::new();
    if spacing > 0.0 && p.ncols() >= 3 {
        for i in 0..p.nrows().saturating_sub(1) {
            let (a, b) = ((p[[i, 0]], p[[i, 1]], p[[i, 2]]), (p[[i + 1, 0]], p[[i + 1, 1]], p[[i + 1, 2]]));
            let (dx, dy) = (b.0 - a.0, b.1 - a.1);
            let l = dx.dhypot(dy);
            if l < 1e-9 {
                continue;
            }
            let (ax, bx_) = if dx.abs() >= dy.abs() { (a.0, b.0) } else { (a.1, b.1) };
            let (lo, hi) = (ax.min(bx_), ax.max(bx_));
            if (bx_ - ax).abs() < 1e-12 {
                continue;
            }
            // (half-open per piece: a mark on a vertex belongs to the piece it starts)
            for k in (lo / spacing).floor() as i64..=(hi / spacing).ceil() as i64 {
                let t = (k as f64 * spacing - ax) / (bx_ - ax);
                if (0.0..1.0).contains(&t) {
                    out.extend_from_slice(&[a.0 + t * dx, a.1 + t * dy, a.2 + t * (b.2 - a.2), dx / l, dy / l]);
                }
            }
        }
    }
    let m = out.len() / 5;
    numpy::ndarray::Array2::from_shape_vec((m, 5), out).unwrap().into_pyarray(py)
}

/// A tall roof's corners for its obstruction lights: for each corner of the smallest rectangle round the ring (pts
/// (n, 2)), the ring's vertex nearest to it, moved `inset` m toward the ring's centre; one point (the centre) when the
/// ring's area is under `min_area`. Distinct points only (a triangle's three). Returns (k, 2).
#[pyfunction]
pub fn roof_corners<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, inset: f64, min_area: f64)
                         -> Bound<'py, PyArray2<f64>> {
    let p = pts.as_array();
    let mut ring: Vec<P2> = (0..p.nrows()).map(|i| (p[[i, 0]], p[[i, 1]])).collect();
    if ring.len() >= 2 && ring[0] == ring[ring.len() - 1] {
        ring.pop();
    }
    let out = corners_core(&ring, inset, min_area);
    let k = out.len();
    let flat: Vec<f64> = out.into_iter().flat_map(|(x, y)| [x, y]).collect();
    numpy::ndarray::Array2::from_shape_vec((k, 2), flat).unwrap().into_pyarray(py)
}

fn corners_core(ring: &[P2], inset: f64, min_area: f64) -> Vec<P2> {
    if ring.len() < 3 {
        return Vec::new();
    }
    let n = ring.len() as f64;
    let c = (ring.iter().map(|q| q.0).sum::<f64>() / n, ring.iter().map(|q| q.1).sum::<f64>() / n);
    if crate::power::ring_area(ring) < min_area {
        return vec![c];
    }
    let Some((u, v, a, b)) = min_box(ring) else { return vec![c] };
    let mut out: Vec<P2> = Vec::new();
    for (ea, eb) in [(a.0, b.0), (a.1, b.0), (a.1, b.1), (a.0, b.1)] {
        let corner = (ea * u.0 + eb * v.0, ea * u.1 + eb * v.1);
        let best = ring.iter().copied().min_by(|p, q| {
            let dp = (p.0 - corner.0).dhypot(p.1 - corner.1);
            let dq = (q.0 - corner.0).dhypot(q.1 - corner.1);
            dp.partial_cmp(&dq).unwrap_or(std::cmp::Ordering::Equal)
        }).unwrap();
        let (dx, dy) = (c.0 - best.0, c.1 - best.1);
        let l = dx.dhypot(dy);
        let pt = if l > inset { (best.0 + dx / l * inset, best.1 + dy / l * inset) } else { c };
        if !out.iter().any(|q| (q.0 - pt.0).dhypot(q.1 - pt.1) < 0.5) {
            out.push(pt);
        }
    }
    out
}

/// Mapped street furniture beside the carriageways: each point (pts (n, 2)) moved straight off the nearest
/// carriageway of seg (m, 5) [ax, ay, bx, by, half width] when it stands on it or within pad of its edge (a bench mapped
/// on the road's line), and the direction toward that carriageway's line when its edge is within reach (NaN beyond).
/// Returns (n, 4) [x, y, yaw degrees toward the road, m from the edge].
#[pyfunction]
pub fn street_points<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, seg: PyReadonlyArray2<'py, f64>, pad: f64,
                          reach: f64) -> Bound<'py, PyArray2<f64>> {
    let p = pts.as_array();
    let segs = Segs::new(&seg.as_array(), 32.0);
    let out = street_core(&p, &segs, pad, reach);
    let n = out.len() / 4;
    numpy::ndarray::Array2::from_shape_vec((n, 4), out).unwrap().into_pyarray(py)
}

fn street_core(p: &numpy::ndarray::ArrayView2<'_, f64>, segs: &Segs, pad: f64, reach: f64) -> Vec<f64> {
    let mut out = Vec::with_capacity(p.nrows() * 4);
    for i in 0..p.nrows() {
        let (mut x, mut y) = (p[[i, 0]], p[[i, 1]]);
        let mut best: Option<(f64, P2, f64)> = None;      // (edge distance, foot, half width)
        for k in segs.near([x - reach, y - reach, x + reach, y + reach]) {
            let q = segs.s[k];
            let (dx, dy) = (q[2] - q[0], q[3] - q[1]);
            let l2 = dx * dx + dy * dy;
            let t = if l2 > 1e-12 { (((x - q[0]) * dx + (y - q[1]) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
            let foot = (q[0] + t * dx, q[1] + t * dy);
            let edge = (x - foot.0).dhypot(y - foot.1) - q[4];
            if best.map_or(true, |b| edge < b.0) {
                best = Some((edge, foot, q[4]));
            }
        }
        let (mut yaw, mut edge) = (f64::NAN, f64::INFINITY);
        if let Some((e, foot, hw)) = best {
            edge = e;
            if e < pad {
                let (ax, ay) = (x - foot.0, y - foot.1);
                let l = ax.dhypot(ay);
                if l > 1e-6 {
                    x = foot.0 + ax / l * (hw + pad);
                    y = foot.1 + ay / l * (hw + pad);
                    edge = pad;
                }
            }
            if edge <= reach {
                yaw = (foot.1 - y).datan2(foot.0 - x).to_degrees().rem_euclid(360.0);
            }
        }
        out.extend_from_slice(&[x, y, yaw, edge]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn square(x0: f64, y0: f64, s: f64) -> Vec<P2> {
        vec![(x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s)]
    }

    fn empty_segs() -> Segs {
        Segs { s: vec![], cell: 32.0, index: HashMap::new() }
    }

    #[test]
    fn grid_fills_a_square_and_keeps_the_box() {
        let rings = vec![square(0.0, 0.0, 100.0)];
        let polys = Polys::new(vec![], 32.0);
        let all = grid_core(&rings, 0.0, [10.0, 4.0], [10.5, 8.0], [0, 0], [0.0, 0.0], 1.0, &empty_segs(), &polys, 1.0,
                            [-1e9, -1e9, 1e9, 1e9]);
        let n = all.len() / 5;
        // along: (100 - 2) / 10.5 -> 9 fit; across: 98 / 8 -> 12 rows (the last one inside)
        assert_eq!(n, 9 * 12);
        let half = grid_core(&rings, 0.0, [10.0, 4.0], [10.5, 8.0], [0, 0], [0.0, 0.0], 1.0, &empty_segs(), &polys, 1.0,
                             [0.0, 0.0, 50.0, 1e9]);
        let other = grid_core(&rings, 0.0, [10.0, 4.0], [10.5, 8.0], [0, 0], [0.0, 0.0], 1.0, &empty_segs(), &polys,
                              1.0, [50.0, 0.0, 1e9, 1e9]);
        assert_eq!(half.len() / 5 + other.len() / 5, n);
    }

    #[test]
    fn grid_aisles_and_avoid() {
        let rings = vec![square(0.0, 0.0, 100.0)];
        let polys = Polys::new(vec![square(40.0, 40.0, 20.0)], 32.0);
        let with = grid_core(&rings, 0.0, [10.0, 4.0], [10.5, 8.0], [3, 4], [6.0, 6.0], 1.0, &empty_segs(), &polys, 1.0,
                             [-1e9, -1e9, 1e9, 1e9]);
        for k in 0..with.len() / 5 {
            let (x, y) = (with[5 * k], with[5 * k + 1]);
            assert!(!(x > 34.0 && x < 66.0 && y > 37.0 && y < 63.0), "item at {x} {y} in the building");
        }
        let no = Polys::new(vec![], 32.0);
        let without = grid_core(&rings, 0.0, [10.0, 4.0], [10.5, 8.0], [3, 4], [6.0, 6.0], 1.0, &empty_segs(), &no, 1.0,
                                [-1e9, -1e9, 1e9, 1e9]);
        assert!(with.len() < without.len());
    }

    #[test]
    fn grid_long_axis() {
        // a long thin strip along y: the items' axis along it
        let rings = vec![vec![(0.0, 0.0), (8.0, 0.0), (8.0, 200.0), (0.0, 200.0)]];
        let polys = Polys::new(vec![], 32.0);
        let got = grid_core(&rings, f64::NAN, [12.0, 2.5], [12.5, 3.0], [0, 0], [0.0, 0.0], 0.5, &empty_segs(), &polys,
                            0.5, [-1e9, -1e9, 1e9, 1e9]);
        assert!(got.len() / 5 >= 30);
        let yaw = got[2];
        assert!((yaw - 90.0).abs() < 1e-6 || (yaw - 270.0).abs() < 1e-6, "yaw {yaw}");
    }

    #[test]
    fn roof_corner_points() {
        let sq = square(0.0, 0.0, 40.0);
        let got = corners_core(&sq, 1.0, 300.0);
        assert_eq!(got.len(), 4);
        for (x, y) in got {
            assert!((x - 0.707).abs() < 0.01 || (x - 39.293).abs() < 0.01, "{x}");
            assert!((y - 0.707).abs() < 0.01 || (y - 39.293).abs() < 0.01, "{y}");
        }
        let small = square(0.0, 0.0, 10.0);
        assert_eq!(corners_core(&small, 1.0, 300.0), vec![(5.0, 5.0)]);
    }

    #[test]
    fn rect_segment_distance() {
        let r = Rect { c: (0.0, 0.0), u: (1.0, 0.0), ha: 2.0, hb: 1.0 };
        assert!((rect_seg(&r, (-5.0, 3.0), (5.0, 3.0)) - 2.0).abs() < 1e-9);
        assert_eq!(rect_seg(&r, (-5.0, 0.0), (5.0, 0.0)), 0.0);
        assert!((rect_seg(&r, (4.0, -5.0), (4.0, 5.0)) - 2.0).abs() < 1e-9);
    }
}
