//! What stands on a flat roof (buildings._clutter; the user, 2026-11-10: "apply high level of detail to all buildings
//! and cities across the world"). Aerial photos of flat roofs (2026-11-10: Chicago's Loop, Berlin-Kreuzberg, Towson MD,
//! a Midtown block) show three kinds of roof:
//! - towers: a mechanical penthouse, a block of a third or so of the roof (lifts' machine rooms, cooling plant behind
//!   louvres), 4-8 m high, near the middle, a few units around it;
//! - mid-rise blocks: a stair / lift bulkhead (about 3-5 x 4-7 m, 3 m high) off the middle and a few air-conditioning
//!   units;
//! - big low roofs (shops, offices, warehouses): fields of rooftop units, about one per 350-450 m2 (Towson's mall roof:
//!   ~20 on ~7,000 m2), a few big ones among small ones.
//! Every box is aligned with the roof's oriented box (axis u), so the placement tests are rectangles in the roof's own
//! (s, t) frame: corners inside the outline, no outline edge through the box grown by the inset, a gap to the boxes
//! already placed, clear of an excluded circle (a water tank's spot). Deterministic from the building's seed.
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

type P = (f64, f64);

/// kind: what the building is (0 houses / flats / mixed, 1 shops / offices, 2 warehouses / factories / big boxes).
pub const KIND_PLAIN: i64 = 0;
pub const KIND_COMMERCIAL: i64 = 1;
pub const KIND_INDUSTRIAL: i64 = 2;

/// Output materials: 0 the building's own facade colour (bulkheads), 1 louvred metal (penthouses), 2 grey units.
const MAT_FACADE: f64 = 0.0;
const MAT_LOUVRE: f64 = 1.0;
const MAT_UNIT: f64 = 2.0;

const TOWER_M: f64 = 45.0;           // m: a tower's penthouse from this height
const TOWER_MIN_M2: f64 = 300.0;
const MID_M: f64 = 11.5;             // m (about four storeys): a stair / lift bulkhead
const MID_MIN_M2: f64 = 120.0;
const BULKHEAD_SHARE: f64 = 0.85;
const UNIT_GAP_M: f64 = 0.8;
const EDGE_M: f64 = 1.0;             // m: inset from the roof's edge (its parapet)

fn splitmix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> f64 {
        self.0 = splitmix(self.0);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
    fn range(&mut self, lo: f64, hi: f64) -> f64 {
        lo + (hi - lo) * self.next()
    }
}

fn inside(ring: &[P], p: P) -> bool {
    let mut c = false;
    let n = ring.len();
    for i in 0..n {
        let (a, b) = (ring[i], ring[(i + 1) % n]);
        if (a.1 > p.1) != (b.1 > p.1) && p.0 < a.0 + (p.1 - a.1) / (b.1 - a.1) * (b.0 - a.0) {
            c = !c;
        }
    }
    c
}

/// Does segment ab meet the rectangle [s0, s1] x [t0, t1] (Liang-Barsky)?
fn seg_hits_rect(a: P, b: P, s0: f64, s1: f64, t0: f64, t1: f64) -> bool {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let (mut lo, mut hi) = (0.0f64, 1.0f64);
    for (p, q) in [(-dx, a.0 - s0), (dx, s1 - a.0), (-dy, a.1 - t0), (dy, t1 - a.1)] {
        if p.abs() < 1e-12 {
            if q < 0.0 {
                return false;
            }
        } else {
            let r = q / p;
            if p < 0.0 {
                lo = lo.max(r);
            } else {
                hi = hi.min(r);
            }
            if lo > hi {
                return false;
            }
        }
    }
    true
}

#[derive(Clone, Copy)]
struct Rect {
    s: f64,
    t: f64,
    hs: f64,
    ht: f64,
}

struct Roof {
    ring: Vec<P>,            // the outline in (s, t)
    placed: Vec<Rect>,
    excl: Option<(P, f64)>,
}

impl Roof {
    fn fits(&self, r: Rect, edge: f64, gap: f64) -> bool {
        let (s0, s1, t0, t1) = (r.s - r.hs - edge, r.s + r.hs + edge, r.t - r.ht - edge, r.t + r.ht + edge);
        for p in [(s0, t0), (s1, t0), (s1, t1), (s0, t1)] {
            if !inside(&self.ring, p) {
                return false;
            }
        }
        let n = self.ring.len();
        for i in 0..n {
            if seg_hits_rect(self.ring[i], self.ring[(i + 1) % n], s0, s1, t0, t1) {
                return false;
            }
        }
        for q in &self.placed {
            if (r.s - q.s).abs() < r.hs + q.hs + gap && (r.t - q.t).abs() < r.ht + q.ht + gap {
                return false;
            }
        }
        if let Some(((cs, ct), cr)) = self.excl {
            let ds = ((cs - r.s).abs() - r.hs).max(0.0);
            let dt = ((ct - r.t).abs() - r.ht).max(0.0);
            if ds * ds + dt * dt < cr * cr {
                return false;
            }
        }
        true
    }
}

fn area(ring: &[P]) -> f64 {
    let n = ring.len();
    let mut a = 0.0;
    for i in 0..n {
        let (p, q) = (ring[i], ring[(i + 1) % n]);
        a += p.0 * q.1 - q.0 * p.1;
    }
    (a / 2.0).abs()
}

/// The boxes on one flat roof: [(s, t, half along u, half across, height, material)] in the roof's frame (see
/// the module notes). hl, hw: the oriented box's half sizes; height: the building's eave height over its ground;
/// excl: (s, t, radius) kept clear, radius <= 0 for none.
pub fn plan(ring: &[P], hl: f64, hw: f64, seed: u64, height: f64, kind: i64, excl: Option<(P, f64)>)
            -> Vec<[f64; 6]> {
    let mut out = Vec::new();
    if ring.len() < 3 || hl < 2.0 || hw < 2.0 {
        return out;
    }
    let a = area(ring);
    let mut rng = Rng(splitmix(seed ^ 0x5EED_0F_200F));
    let mut roof = Roof { ring: ring.to_vec(), placed: Vec::new(), excl };
    let put = |roof: &mut Roof, r: Rect, h: f64, mat: f64, out: &mut Vec<[f64; 6]>| {
        roof.placed.push(r);
        out.push([r.s, r.t, r.hs, r.ht, h, mat]);
    };
    let tower = height >= TOWER_M && a >= TOWER_MIN_M2;
    let mid = !tower && height >= MID_M && a >= MID_MIN_M2;
    if tower {
        // the penthouse: 45-65 % of each side (20-40 % of the box), a little off the middle, shrunk until it fits
        let (fs, ft) = (rng.range(0.45, 0.65), rng.range(0.45, 0.65));
        let (os, ot) = (rng.range(-0.25, 0.25), rng.range(-0.25, 0.25));
        let h = rng.range(4.5, 7.5) * (0.75 + 0.25 * (height / 150.0).min(1.0));
        let mut k = 1.0;
        for _ in 0..6 {
            let r = Rect { s: os * (hl * (1.0 - fs * k)), t: ot * (hw * (1.0 - ft * k)), hs: hl * fs * k,
                           ht: hw * ft * k };
            if r.hs >= 2.0 && r.ht >= 2.0 && roof.fits(r, EDGE_M + 1.0, 0.0) {
                put(&mut roof, r, h, MAT_LOUVRE, &mut out);
                break;
            }
            k *= 0.8;
        }
    } else if mid && rng.next() < BULKHEAD_SHARE {
        // the stair / lift bulkhead: its long side along the roof's or across it, toward one end
        let (mut hs, mut ht) = (rng.range(1.5, 2.5), rng.range(2.0, 3.5));
        if rng.next() < 0.5 {
            std::mem::swap(&mut hs, &mut ht);
        }
        let h = rng.range(2.7, 3.6);
        for _ in 0..8 {
            let r = Rect { s: rng.range(-1.0, 1.0) * (hl - hs - EDGE_M).max(0.0),
                           t: rng.range(-1.0, 1.0) * (hw - ht - EDGE_M).max(0.0), hs, ht };
            if roof.fits(r, EDGE_M, 0.0) {
                put(&mut roof, r, h, MAT_FACADE, &mut out);
                break;
            }
        }
    }
    // the units: one per `per` m2 of roof, up to `cap`
    let (per, cap, big) = match kind {
        KIND_INDUSTRIAL => (450.0, 24usize, 0.45),
        KIND_COMMERCIAL => (380.0, 24usize, 0.35),
        _ if tower => (500.0, 4usize, 0.4),
        _ if mid => (350.0, 8usize, 0.25),
        _ => (600.0, 3usize, 0.25),
    };
    if a < 150.0 && !mid {
        return out;
    }
    let free = (a - roof.placed.iter().map(|r| 4.0 * r.hs * r.ht).sum::<f64>()).max(0.0);
    let want = ((free / per).round() as usize).clamp(if a >= 250.0 { 1 } else { 0 }, cap);
    let mut tries = 0;
    let mut got = 0;
    while got < want && tries < want * 5 + 4 {
        tries += 1;
        let large = rng.next() < big;
        let (mut hs, mut ht, h) = if large {
            (rng.range(1.2, 2.2), rng.range(0.9, 1.3), rng.range(1.3, 2.0))
        } else {
            (rng.range(0.55, 0.9), rng.range(0.45, 0.8), rng.range(0.8, 1.3))
        };
        if rng.next() < 0.5 {
            std::mem::swap(&mut hs, &mut ht);
        }
        let r = Rect { s: rng.range(-1.0, 1.0) * (hl - hs - EDGE_M).max(0.0),
                       t: rng.range(-1.0, 1.0) * (hw - ht - EDGE_M).max(0.0), hs, ht };
        if roof.fits(r, EDGE_M * 0.8, UNIT_GAP_M) {
            put(&mut roof, r, h, MAT_UNIT, &mut out);
            got += 1;
        }
    }
    out
}

/// buildings._clutter: the boxes on a flat roof (plan), ring (n, 2) in the frame, its oriented box (cx, cy, ux, uy,
/// hl, hw); excl (s, t, r) in the roof's frame (r <= 0: none). Returns (k, 6): s, t, half along u, half across,
/// height, material (0 facade, 1 louvres, 2 units).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn roof_kit<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, cx: f64, cy: f64, ux: f64, uy: f64, hl: f64,
                     hw: f64, seed: u64, height: f64, kind: i64, excl: (f64, f64, f64))
                     -> Bound<'py, PyArray2<f64>> {
    let r = ring.as_array();
    let (vx, vy) = (-uy, ux);
    let st: Vec<P> = if r.ncols() >= 2 {
        r.rows().into_iter().map(|p| {
            let (dx, dy) = (p[0] - cx, p[1] - cy);
            (dx * ux + dy * uy, dx * vx + dy * vy)
        }).collect()
    } else {
        Vec::new()
    };
    let ex = if excl.2 > 0.0 { Some(((excl.0, excl.1), excl.2)) } else { None };
    let boxes = py.detach(|| plan(&st, hl, hw, seed, height, kind, ex));
    let mut a = numpy::ndarray::Array2::<f64>::zeros((boxes.len(), 6));
    for (i, b) in boxes.iter().enumerate() {
        for j in 0..6 {
            a[[i, j]] = b[j];
        }
    }
    a.into_pyarray(py)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(hl: f64, hw: f64) -> Vec<P> {
        vec![(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]
    }

    fn inside_all(ring: &[P], b: &[f64; 6]) -> bool {
        [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)].iter()
            .all(|(a, c)| inside(ring, (b[0] + a * b[2], b[1] + c * b[3])))
    }

    #[test]
    fn tower_has_a_penthouse_inside() {
        let ring = rect(20.0, 15.0);
        let got = plan(&ring, 20.0, 15.0, 7, 120.0, KIND_COMMERCIAL, None);
        assert!(got.iter().any(|b| b[5] == MAT_LOUVRE));
        assert!(got.iter().all(|b| inside_all(&ring, b)));
    }

    #[test]
    fn units_do_not_overlap() {
        let ring = rect(60.0, 40.0);
        let got = plan(&ring, 60.0, 40.0, 3, 7.0, KIND_INDUSTRIAL, None);
        assert!(got.len() >= 10, "{}", got.len());
        for (i, a) in got.iter().enumerate() {
            for b in &got[i + 1..] {
                assert!((a[0] - b[0]).abs() >= a[2] + b[2] || (a[1] - b[1]).abs() >= a[3] + b[3]);
            }
        }
    }

    #[test]
    fn l_shape_keeps_out_of_the_notch() {
        // an L: the box's upper-right quarter is outside
        let ring = vec![(-20.0, -20.0), (20.0, -20.0), (20.0, 0.0), (0.0, 0.0), (0.0, 20.0), (-20.0, 20.0)];
        for seed in 0..50 {
            for b in plan(&ring, 20.0, 20.0, seed, 20.0, KIND_COMMERCIAL, None) {
                assert!(inside_all(&ring, &b));
                assert!(!(b[0] + b[2] > 0.0 && b[1] + b[3] > 0.0), "in the notch: {:?}", b);
            }
        }
    }

    #[test]
    fn excluded_circle_is_kept_clear() {
        let ring = rect(15.0, 12.0);
        for seed in 0..50 {
            for b in plan(&ring, 15.0, 12.0, seed, 25.0, KIND_PLAIN, Some(((0.0, 0.0), 3.0))) {
                let ds = (b[0].abs() - b[2]).max(0.0);
                let dt = (b[1].abs() - b[3]).max(0.0);
                assert!(ds * ds + dt * dt >= 9.0 - 1e-9);
            }
        }
    }

    #[test]
    fn small_house_roof_gets_nothing() {
        assert!(plan(&rect(5.0, 4.0), 5.0, 4.0, 1, 6.0, KIND_PLAIN, None).is_empty());
    }
}
