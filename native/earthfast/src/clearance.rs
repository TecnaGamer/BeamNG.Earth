//! Roadside furniture against the carriageways and decks (furniture.py RoadClearance / under_decks, PR #629): the
//! furniture's whole collider boxes kept clear of every carriageway segment at its level, and props / wires kept out
//! of the decks over them. PR #629 wrote both in numpy; per call they were a few array ops on a handful of segments
//! each, ~2.3 s more over 36 Los Angeles road tiles (tileprobe, 2026-11-04). The same rules here, a segment grid each.
use crate::dm::Dm;

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

struct Grid {
    cell: f64,
    buckets: HashMap<(i64, i64), Vec<u32>>,
}

impl Grid {
    fn new(cell: f64) -> Self {
        Grid { cell: cell.max(1.0), buckets: HashMap::new() }
    }

    fn add(&mut self, id: u32, x0: f64, y0: f64, x1: f64, y1: f64) {
        let (i0, i1) = ((x0.min(x1) / self.cell).floor() as i64, (x0.max(x1) / self.cell).floor() as i64);
        let (j0, j1) = ((y0.min(y1) / self.cell).floor() as i64, (y0.max(y1) / self.cell).floor() as i64);
        // (a long segment's cells along its bounding box; capped so a stray kilometre-long one stays cheap)
        if (i1 - i0 + 1) * (j1 - j0 + 1) > 4096 {
            for i in i0..=i1 {
                for j in j0..=j1 {
                    if seg_cell(x0, y0, x1, y1, i, j, self.cell) {
                        self.buckets.entry((i, j)).or_default().push(id);
                    }
                }
            }
            return;
        }
        for i in i0..=i1 {
            for j in j0..=j1 {
                self.buckets.entry((i, j)).or_default().push(id);
            }
        }
    }

    fn query(&self, x0: f64, y0: f64, x1: f64, y1: f64, seen: &mut Vec<u32>, stamp: u32, out: &mut Vec<u32>) {
        out.clear();
        let (i0, i1) = ((x0 / self.cell).floor() as i64, (x1 / self.cell).floor() as i64);
        let (j0, j1) = ((y0 / self.cell).floor() as i64, (y1 / self.cell).floor() as i64);
        for i in i0..=i1 {
            for j in j0..=j1 {
                if let Some(v) = self.buckets.get(&(i, j)) {
                    for &s in v {
                        if seen[s as usize] != stamp {
                            seen[s as usize] = stamp;
                            out.push(s);
                        }
                    }
                }
            }
        }
    }
}

/// Does the segment come within the grid cell (i, j)? (its distance to the cell's box under half a cell's diagonal)
fn seg_cell(x0: f64, y0: f64, x1: f64, y1: f64, i: i64, j: i64, c: f64) -> bool {
    let lo = [i as f64 * c, j as f64 * c];
    let hi = [lo[0] + c, lo[1] + c];
    box_distance2([x0, y0], [x1, y1], lo, hi) <= 0.0
}

/// Squared distance of the 2D segment a..b to the closed rectangle lo..hi (furniture._box_distance2).
fn box_distance2(a: [f64; 2], b: [f64; 2], lo: [f64; 2], hi: [f64; 2]) -> f64 {
    let d = [b[0] - a[0], b[1] - a[1]];
    let d2 = (d[0] * d[0] + d[1] * d[1]).max(1e-20);
    let out2 = |p: [f64; 2]| {
        let mut s = 0.0;
        for k in 0..2 {
            let v = (lo[k] - p[k]).max(p[k] - hi[k]).max(0.0);
            s += v * v;
        }
        s
    };
    let mut best = out2(a).min(out2(b));
    let (mut enter, mut leave) = (0.0f64, 1.0f64);
    for k in 0..2 {
        if d[k].abs() < 1e-12 {
            if a[k] < lo[k] || a[k] > hi[k] {
                leave = f64::NEG_INFINITY;
            }
        } else {
            let (t0, t1) = ((lo[k] - a[k]) / d[k], (hi[k] - a[k]) / d[k]);
            enter = enter.max(t0.min(t1));
            leave = leave.min(t0.max(t1));
        }
    }
    if enter <= leave {
        return 0.0;
    }
    for c in [[lo[0], lo[1]], [lo[0], hi[1]], [hi[0], lo[1]], [hi[0], hi[1]]] {
        let v = [c[0] - a[0], c[1] - a[1]];
        let t = ((v[0] * d[0] + v[1] * d[1]) / d2).clamp(0.0, 1.0);
        let e = [v[0] - t * d[0], v[1] - t * d[1]];
        best = best.min(e[0] * e[0] + e[1] * e[1]);
    }
    best
}

/// furniture.RoadClearance's segments: a, b (n, 3) (z NaN: a 2D line, blocking at every level), half widths and the
/// structure's depth under each (0 for a road on the ground).
#[pyclass(module = "earthfast")]
pub struct Clearance {
    a: Vec<[f64; 3]>,
    b: Vec<[f64; 3]>,
    half: Vec<f64>,
    bottom: Vec<f64>,
    max_half: f64,
    grid: Grid,
    seen: std::sync::Mutex<(Vec<u32>, u32)>,
}

#[pymethods]
impl Clearance {
    #[new]
    fn new(a: PyReadonlyArray2<f64>, b: PyReadonlyArray2<f64>, half: PyReadonlyArray1<f64>,
           bottom: PyReadonlyArray1<f64>, cell: f64) -> PyResult<Self> {
        let (a, b, half, bottom) = (a.as_array(), b.as_array(), half.as_array(), bottom.as_array());
        let n = a.nrows();
        let mut grid = Grid::new(cell);
        let (mut av, mut bv) = (Vec::with_capacity(n), Vec::with_capacity(n));
        for i in 0..n {
            let p = [a[[i, 0]], a[[i, 1]], a[[i, 2]]];
            let q = [b[[i, 0]], b[[i, 1]], b[[i, 2]]];
            grid.add(i as u32, p[0], p[1], q[0], q[1]);
            av.push(p);
            bv.push(q);
        }
        let half: Vec<f64> = half.iter().copied().collect();
        let max_half = half.iter().copied().fold(0.0, f64::max);
        Ok(Clearance { a: av, b: bv, half, bottom: bottom.iter().copied().collect(), max_half, grid,
                       seen: std::sync::Mutex::new((vec![0; n], 0)) })
    }

    /// Does every box (k, 6: x0 y0 z0 x1 y1 z1, scaled, in the object's frame) of an object at (ox, oy, oz) turned
    /// yaw (radians) keep margin m clear of every segment's half width, at the segment's level (its stretch between the
    /// box's bottom - headroom and its top + the structure's depth)? furniture.RoadClearance.clear's rule.
    fn clear(&self, ox: f64, oy: f64, oz: f64, yaw: f64, boxes: PyReadonlyArray2<f64>, margin: f64, headroom: f64)
             -> bool {
        let boxes = boxes.as_array();
        let (c, s) = (yaw.dcos(), yaw.dsin());
        let reach = self.max_half + margin;
        let mut guard = self.seen.lock().unwrap();
        let (seen, stamp) = &mut *guard;
        let mut pick = Vec::new();
        for k in 0..boxes.nrows() {
            let lo = [boxes[[k, 0]], boxes[[k, 1]], boxes[[k, 2]]];
            let hi = [boxes[[k, 3]], boxes[[k, 4]], boxes[[k, 5]]];
            let (mut mnx, mut mny, mut mxx, mut mxy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
            for (x, y) in [(lo[0], lo[1]), (lo[0], hi[1]), (hi[0], lo[1]), (hi[0], hi[1])] {
                let wx = x * c - y * s + ox;
                let wy = x * s + y * c + oy;
                mnx = mnx.min(wx);
                mny = mny.min(wy);
                mxx = mxx.max(wx);
                mxy = mxy.max(wy);
            }
            *stamp = stamp.wrapping_add(1);
            if *stamp == 0 {
                seen.iter_mut().for_each(|v| *v = 0);
                *stamp = 1;
            }
            self.grid.query(mnx - reach, mny - reach, mxx + reach, mxy + reach, seen, *stamp, &mut pick);
            for &i in &pick {
                let i = i as usize;
                let (a, b) = (self.a[i], self.b[i]);
                let (za, zb) = (a[2], b[2]);
                let known = za.is_finite() && zb.is_finite();
                let dz = if known { zb - za } else { 0.0 };
                let zlow = oz + lo[2] - headroom;
                let zhigh = oz + hi[2] + self.bottom[i];
                let flat = dz.abs() < 1e-12;
                let (enter, leave) = if flat || !known {
                    (0.0, 1.0)
                } else {
                    let (t0, t1) = ((zlow - za) / dz, (zhigh - za) / dz);
                    (t0.min(t1).max(0.0), t0.max(t1).min(1.0))
                };
                let valid = (!known || !flat || (za >= zlow && za <= zhigh)) && enter <= leave;
                if !valid {
                    continue;
                }
                let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                let local = |t: f64| {
                    let vx = a[0] + t * dx - ox;
                    let vy = a[1] + t * dy - oy;
                    [vx * c + vy * s, -vx * s + vy * c]
                };
                let r = self.half[i] + margin;
                if box_distance2(local(enter), local(leave), [lo[0], lo[1]], [hi[0], hi[1]]) < r * r {
                    return false;
                }
            }
        }
        true
    }

    fn __len__(&self) -> usize {
        self.a.len()
    }
}

/// furniture.under_decks' decks: segments a, b (n, 3) with their reach (half width + DECK_SIDE_M).
#[pyclass(module = "earthfast")]
pub struct DeckLevels {
    a: Vec<[f64; 3]>,
    b: Vec<[f64; 3]>,
    reach: Vec<f64>,
    max_reach: f64,
    grid: Grid,
}

impl DeckLevels {
    /// Every deck surface's z over (x, y) (each segment within its reach), into out.
    fn levels(&self, x: f64, y: f64, pick: &mut Vec<u32>, seen: &mut Vec<u32>, stamp: &mut u32, out: &mut Vec<f64>) {
        out.clear();
        *stamp = stamp.wrapping_add(1);
        if *stamp == 0 {
            seen.iter_mut().for_each(|v| *v = 0);
            *stamp = 1;
        }
        let r = self.max_reach;
        self.grid.query(x - r, y - r, x + r, y + r, seen, *stamp, pick);
        for &i in pick.iter() {
            let i = i as usize;
            let (a, b) = (self.a[i], self.b[i]);
            let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((x - a[0]) * dx + (y - a[1]) * dy) / l2).clamp(0.0, 1.0);
            let (qx, qy) = (a[0] + t * dx, a[1] + t * dy);
            if (qx - x).dhypot(qy - y) < self.reach[i] {
                out.push(a[2] + t * (b[2] - a[2]));
            }
        }
    }
}

#[pymethods]
impl DeckLevels {
    #[new]
    fn new(a: PyReadonlyArray2<f64>, b: PyReadonlyArray2<f64>, reach: PyReadonlyArray1<f64>, cell: f64)
           -> PyResult<Self> {
        let (a, b, reach) = (a.as_array(), b.as_array(), reach.as_array());
        let mut grid = Grid::new(cell);
        let (mut av, mut bv) = (Vec::new(), Vec::new());
        for i in 0..a.nrows() {
            let p = [a[[i, 0]], a[[i, 1]], a[[i, 2]]];
            let q = [b[[i, 0]], b[[i, 1]], b[[i, 2]]];
            grid.add(i as u32, p[0], p[1], q[0], q[1]);
            av.push(p);
            bv.push(q);
        }
        let reach: Vec<f64> = reach.iter().copied().collect();
        let max_reach = reach.iter().copied().fold(0.0, f64::max);
        Ok(DeckLevels { a: av, b: bv, reach, max_reach, grid })
    }

    /// Per prop (x, y, base z0, top z1): does it reach up into a deck over it (z0 < deck z and z1 > deck z - headroom)?
    fn blocked(&self, x: PyReadonlyArray1<f64>, y: PyReadonlyArray1<f64>, z0: PyReadonlyArray1<f64>,
               z1: PyReadonlyArray1<f64>, headroom: f64) -> Vec<bool> {
        let (x, y, z0, z1) = (x.as_array(), y.as_array(), z0.as_array(), z1.as_array());
        let (mut pick, mut seen, mut stamp, mut zs) = (Vec::new(), vec![0u32; self.a.len()], 0u32, Vec::new());
        (0..x.len()).map(|k| {
            self.levels(x[k], y[k], &mut pick, &mut seen, &mut stamp, &mut zs);
            zs.iter().any(|&z| z0[k] < z && z1[k] > z - headroom)
        }).collect()
    }

    /// Does any point (x, y, z) of a wire pass a deck between deck z - below and deck z + above?
    fn wire_hit(&self, x: PyReadonlyArray1<f64>, y: PyReadonlyArray1<f64>, z: PyReadonlyArray1<f64>, below: f64,
                above: f64) -> bool {
        let (x, y, z) = (x.as_array(), y.as_array(), z.as_array());
        let (mut pick, mut seen, mut stamp, mut zs) = (Vec::new(), vec![0u32; self.a.len()], 0u32, Vec::new());
        (0..x.len()).any(|k| {
            self.levels(x[k], y[k], &mut pick, &mut seen, &mut stamp, &mut zs);
            zs.iter().any(|&d| d - below < z[k] && z[k] < d + above)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn box_distance() {
        assert_eq!(box_distance2([-5.0, 0.5], [5.0, 0.5], [0.0, 0.0], [1.0, 1.0]), 0.0);     // through it
        assert!((box_distance2([-5.0, 3.0], [5.0, 3.0], [0.0, 0.0], [1.0, 1.0]) - 4.0).abs() < 1e-9);   // 2 m over
        assert!((box_distance2([3.0, 3.0], [4.0, 4.0], [0.0, 0.0], [1.0, 1.0]) - 8.0).abs() < 1e-9);    // corner
        assert!(seg_cell(-5.0, 0.5, 5.0, 0.5, 0, 0, 1.0) && !seg_cell(-5.0, 3.0, 5.0, 3.0, 0, 0, 1.0));
    }
}
